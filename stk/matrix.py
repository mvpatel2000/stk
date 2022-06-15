import numpy as np
import torch

# 2. Add heavyweight (data) validation helper.
# 3. Add construction helpers
# 4. Test with custom kernels.
# 5. Make indentation consistent
# 6. Replace asserts with descriptive errors.

##
### Validation helpers.
##


def _validate_matrix(shape, data, row_indices, column_indices, offsets):
    # Data should be [nnz, block_size, block_size]
    if data.dim() == 1:
        data = torch.reshape(data, [data.numel(), 1, 1])

    # Blocks should be square.
    if data.shape[-2] != data.shape[-1]:
        raise ValueError(
            "Expected square blocking in data. "
            f"Got block shape {[data.shape[-2], data.shape[-1]]}")

    # Flatten batch dimensions on data - original shape preserved
    # in shape argument.
    block_size = data.shape[-1]
    data = data.view([-1, block_size, block_size])

    if data.dim() != 3:
        raise ValueError(
            "Expected 3D shape for data (nnz, block, block). "
            f"Got shape {data.dim()}D shape.")

    block_size = data.shape[1]
    if shape[-2] % block_size != 0 or shape[-1] % block_size != 0:
        raise ValueError(
            "Matrix shape must be dividible by blocking. "
            f"Got shape {shape} with "
            f"{[block_size, block_size]} blocking.")

    if np.prod(shape) < data.numel():
        raise ValueError(
            "Invalid matrix. Number of nonzeros exceeds matrix capacity "
            f"({data.numel()} v. {np.prod(shape)})")

    if row_indices.dim() != 1:
        raise ValueError(
            f"Expected 1D row_indices. Got {row_indices.dim()}D row_indices.")

    if column_indices.dim() != 1:
        raise ValueError(
            f"Expected 1D column_indices. Got {column_indices.dim()}D column_indices.")

    if offsets.dim() != 1:
        raise ValueError(
            f"Expected 1D offsets. Got {offsets.dim()}D offsets.")

    if row_indices.numel() != data.shape[0]:
        raise ValueError(
            "Expected 1 index per nonzero block. "
            f"Got {row_indices.numel()} row_indices for {data.shape[0]} blocks")

    if column_indices.numel() != data.shape[0]:
        raise ValueError(
            "Expected 1 index per nonzero block. "
            f"Got {column_indices.numel()} column_indices for {data.shape[0]} blocks")

    block_rows = np.prod(shape[:-1]) / block_size
    if offsets.numel() != block_rows + 1:
        raise ValueError(
            "Expected one offset per block row plus one. "
            f"Got {offsets.numel()} offsets with {block_rows} block rows.")

    is_cuda = (data.is_cuda and
               row_indices.is_cuda and
               column_indices.is_cuda and
               offsets.is_cuda)
    is_cpu = (not data.is_cuda and
              not row_indices.is_cuda and
              not column_indices.is_cuda and
              not offsets.is_cuda)
    if not (is_cuda or is_cpu):
        raise ValueError(
            "Expected data & meta-data on common device. "
            f"Got data on {data.device}, row_indices on {row_indices.device} "
            f"column_indices on {column_indices.device} and "
            f"offsets on {offsets.device}.")

    if data.dtype != torch.float16:
        raise ValueError(
            f"Expected float16 data. Got {data.dtype} data.")
    if row_indices.dtype != torch.int16:
        raise ValueError(
            f"Expected int16 row_indices. Got {row_indices.dtype} row_indices.")
    if column_indices.dtype != torch.int16:
        raise ValueError(
            f"Expected int16 column_indices. Got {column_indices.dtype} column_indices.")
    if offsets.dtype != torch.int32:
        raise ValueError(
            f"Expected int32 offsets. Got {offsets.dtype} offsets.")
    return data


class Matrix(object):
    """A matrix stored in sparse format.

    Underlying format is block compressed sparse row (BCSR).

    TODO(tgale): Make this mirror torch.Tensor API as much as possible.
    """

    def __init__(self,
                 size,
                 data,
                 row_indices,
                 column_indices,
                 offsets,
                 validate=True):
        self._size = size
        self._data = data
        self._row_indices = row_indices
        self._column_indices = column_indices
        self._offsets = offsets

        # Lightweight validation.
        if validate:
            self._data = _validate_matrix(self._size,
                                          data,
                                          self._row_indices,
                                          self._column_indices,
                                          self._offsets)

        self._transposed = False


    def validate(self):
        _validate_matrix(self._size,
                         self._data,
                         self._row_indices,
                         self._column_indices,
                         self._offsets)

        # TODO(tgale): Add heavyweight data validation.

    def to(self, device):
        # TODO(tgale): Handle type conversions here. We
        # need to set the appropriate meta-data type for
        # the given floating-point type.
        self._data = self._data.to(device)
        self._row_indices = self._row_indices.to(device)
        self._column_indices = self._column_indices.to(device)
        self._offsets = self._offsets.to(device)
        return self

    def cuda(self):
        return self.to(torch.cuda.current_device())

    def clone(self):
        return Matrix(
            self.size(),
            self.data.clone(),
            self.row_indices.clone(),
            self.column_indices.clone(),
            self.offsets.clone())

    def t(self):
        if self.dim() != 2:
            raise ValueError(
                "t() expects a tensor with <= 2 dimensions, "
                f"but self is {self.dim()}D.")
        out = Matrix(self.size(),
                     self.data,
                     self.row_indices,
                     self.column_indices,
                     self.offsets)
        out._transposed = not self._transposed
        out._size = torch.Size((self._size[1], self._size[0]))
        return out

    def contiguous(self):
        raise ValueError("Not yet implemented.")

    def is_contiguous(self):
        return not self._transposed

    @property
    def is_cuda(self):
        return self._data.is_cuda

    @property
    def device(self):
        return self._data.device

    def size(self):
        return self._size

    @property
    def shape(self):
        return self.size()

    def dim(self):
        return len(self._size)

    @property
    def data(self):
        return self._data

    @property
    def row_indices(self):
        return self._row_indices

    @property
    def column_indices(self):
        return self._column_indices

    @property
    def offsets(self):
        return self._offsets

    @property
    def dtype(self):
        return self.data.dtype

    @property
    def nnz(self):
        return self.data.numel()

    @property
    def blocking(self):
        return self.data.shape[1]

    @property
    def requires_grad(self):
        return self.data.requires_grad

    def requires_grad_(self, x):
        self.data.requires_grad_(x)
        return self

    def view(self, *shape):
        assert self.is_contiguous()
        if shape[-1] != self.size()[-1]:
            raise ValueError(
                "Can't change view on compressed dimension. "
                f"{self.size()[-1]} v. {shape[-1]}.")
        if np.prod(shape) != np.prod(self.size()):
            raise ValueError(
                "Mismatch in numel of Matrix and new shape. "
                f"{np.prod(self.size())} v. {np.prod(shape)}")
        return Matrix(shape,
                      self.data,
                      self.row_indices,
                      self.column_indices,
                      self.offsets)

    @property
    def grad(self):
        # TODO(tgale): Make sure this mirrors torch.Tensor
        # behavior in the case where we ask for the gradient
        # of a non-contiguous tensor.
        size = self.size()
        if not self.is_contiguous():
            size = torch.Size((size[1], size[0]))
        out = Matrix(size,
                     self.data.grad,
                     self.row_indices,
                     self.column_indices,
                     self.offsets)
        return out if self.is_contiguous() else out.t()
