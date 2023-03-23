import unittest

from absl.testing import parameterized
import stk
import numpy as np
import torch
import triton_kernels
from megablocks import ops
import triton.ops

def print_log_benchmark(name, arguments, time, std):
    print("="*60)
    print(f"{name} Benchmark")
    print("Benchmark Parameters:")
    for (key, value) in arguments.items():
        print(f"{key} = {value}")
    print("Results:")
    print("mean time = {:.2f}ms, std time = {:.2f}ms".format(time, std))
    print("="*60)


def benchmark_function(fn, iterations=100, warmup=10):
    # Warmup iterations.
    for _ in range(warmup):
        fn()

    times = []
    for i in range(iterations):
        start = torch.cuda.Event(enable_timing=True)
        end = torch.cuda.Event(enable_timing=True)

        start.record()
        fn()
        # with torch.autograd.profiler.profile(with_stack=True, use_cuda=True) as prof:
        #     fn()
        # print(prof.key_averages(group_by_stack_n=1).table(sort_by="self_cuda_time_total"))
        end.record()
        
        torch.cuda.synchronize()
        times.append(start.elapsed_time(end))
    return np.mean(times), np.std(times)


# Calling tensor.t() calls tensor.transpose(0, 1) which calls
# torch.as_strided(...). Circumvent this chain to avoid an overhead
# this adds.
def transpose_view(x):
    return torch.as_strided(
        x, (x.shape[1], x.shape[0]), (x.stride()[1], x.stride()[0]))


_MATMUL_TESTS = (
    (64 * 1024, 512, 2048, 64),
    (32 * 1024, 768, 3072, 64),
    (8 * 1024, 1024, 4096, 64),
)


def log_benchmark(name, arguments, time, std, flops):
    print_log_benchmark(name, arguments, time, std)
    print("flops = {:.2f}B".format(flops / 1e9))
    print("throughput = {:.2f}T".format(flops / 1e9 / time))
    print("="*60)


class MatmulBenchmark(parameterized.TestCase):

    def build_sparse_matrix(self, x, padded_bins, fhs, ne):
        blocking = 128
        padded_tokens, _ = x.size()
        assert padded_tokens % blocking == 0
        assert fhs % blocking == 0

        # Offsets for the sparse matrix. All rows have the
        # same number of nonzero blocks dictated by the
        # dimensionality of a single expert.
        block_rows = padded_tokens // blocking
        blocks_per_row = fhs // blocking
        offsets = torch.arange(
            0,
            block_rows * blocks_per_row + 1,
            blocks_per_row,
            dtype=torch.int32,
            device=x.device)

        # Indices for the sparse matrix. The indices for
        # the intermediate matrix are dynamic depending
        # on the mapping of tokens to experts.
        column_indices = ops.topology(padded_bins,
                                      blocking,
                                      block_rows,
                                      blocks_per_row)
        data = torch.empty(
            column_indices.numel(),
            blocking,
            blocking,
            dtype=torch.float16,
            device=x.device)
        shape = (padded_tokens, fhs * ne)
        row_indices = stk.ops.row_indices(
            shape, data, offsets, column_indices)
        return stk.Matrix(shape,
                          data,
                          row_indices,
                          column_indices,
                          offsets)

    def build_input_matrix(self, sl, hs, ne):
        x = torch.randn((sl, hs)).cuda().half()

        # Assign tokens to experts uniformly.
        top_expert = torch.arange(0, sl).cuda().int() % ne

        bin_ids, indices = ops.sort(top_expert)
        tokens_per_expert = ops.histogram(top_expert, ne)
        padded_tokens_per_expert = ops.round_up(tokens_per_expert, 128)
        padded_bins = ops.inclusive_cumsum(padded_tokens_per_expert, 0)
        bins = ops.inclusive_cumsum(tokens_per_expert, 0)
        out = ops.padded_gather(x, indices, bin_ids, bins, padded_bins)
        return out, padded_bins

    def build_weight_matrix(self, ne, hs, fhs):
        return torch.randn((hs, ne * fhs)).cuda().half()

    @parameterized.parameters(*_MATMUL_TESTS)
    def testFFN_Linear0_Fwd_SDD_STK(self, sl, hs, fhs, ne):
        x, padded_bins = self.build_input_matrix(sl, hs, ne)
        w = self.build_weight_matrix(ne, hs, fhs).t().contiguous()
        topo = self.build_sparse_matrix(x, padded_bins, fhs, ne)
        w = transpose_view(w)

        benchmark = lambda: stk.ops.sdd(x, w, topo)
        mean_t, std_t = benchmark_function(benchmark)
        arguments = {
            "sequence_length": sl,
            "hidden_size": hs,
            "ffn_hidden_size": fhs,
            "num_experts": ne
        }
        log_benchmark("0::Fwd::SDD::STK", arguments, mean_t, std_t,
                      x.numel() * fhs * 2)
    
    @parameterized.parameters(*_MATMUL_TESTS)
    def testFFN_Linear0_Fwd_SDD_Triton(self, sl, hs, fhs, ne):
        x, padded_bins = self.build_input_matrix(sl, hs, ne)
        w = self.build_weight_matrix(ne, hs, fhs).t().contiguous()
        topo = self.build_sparse_matrix(x, padded_bins, fhs, ne)
        w = transpose_view(w)

        benchmark = lambda: triton_kernels.matmul(x, w, topo)
        mean_t, std_t = benchmark_function(benchmark)
        arguments = {
            "sequence_length": sl,
            "hidden_size": hs,
            "ffn_hidden_size": fhs,
            "num_experts": ne
        }
        log_benchmark("0::Fwd::SDD::Triton", arguments, mean_t, std_t,
                        x.numel() * fhs * 2)
    
    @parameterized.parameters(*_MATMUL_TESTS)
    def testFFN_Linear0_Fwd_DDD_NT(self, sl, hs, fhs, ne):
        assert (sl % ne) == 0
        x = torch.randn((ne, sl // ne, hs)).cuda().half()
        w = torch.randn((ne, hs, fhs)).cuda().half()

        w = w.transpose(1, 2).contiguous()
        w = w.transpose(1, 2)
        
        benchmark = lambda: torch.bmm(x, w)
        mean_t, std_t = benchmark_function(benchmark)
        arguments = {
            "sequence_length": sl,
            "hidden_size": hs,
            "ffn_hidden_size": fhs,
            "num_experts": ne
        }
        log_benchmark("0::Fwd:DDD::NT", arguments, mean_t, std_t,
                      x.numel() * fhs * 2)

if __name__ == '__main__':
    unittest.main()