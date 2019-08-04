import numpy as np
import dask.array as da
from pylops import LinearOperator as pLinearOperator


class LinearOperator(pLinearOperator):
    """Common interface for performing matrix-vector products.

    This class is an overload of the
    :py:class:`pylops.LinearOperator` class. It adds
    functionalities for distributed operators; specifically, it allows users
    choosing whether to compute forward and adjoint or simply define their
    graphs.

    In order to avoid the input vectors to be converted to ``numpy`` array
    by ``matvec`` and ``rmatvec`` of the parent class, those methods are
    overwritten here to simply call their private methods
    ``_matvec`` and ``_rmatvec`` without any prior check on the input vectors.

    .. note:: End users of PyLops should not use this class directly but simply
      use operators that are already implemented. This class is meant for
      developers and it has to be used as the parent class of any new operator
      developed within PyLops-distibuted. Find more details regarding
      implementation of new operators at :ref:`addingoperator`.

    Parameters
    ----------
    shape : :obj:`tuple`
        Operator shape
    dtype : :obj:`torch.dtype`, optional
        Type of elements in input array.
    Op : :obj:`pylops.LinearOperator`
        Operator to wrap in ``LinearOperator``
        (if ``None``, self must implement ``_matvec_`` and  ``_rmatvec_``)
    explicit : :obj:`bool`
        Operator contains a matrix that can be solved explicitly
        (``True``) or not (``False``)
    compute : :obj:`tuple`, optional
        Compute the outcome of forward and adjoint or simply define the graph
        and return a :obj:`dask.array`
    todask : :obj:`tuple`, optional
        Apply :func:`dask.array.from_array` to model and data before applying
        forward and adjoint respectively

    """
    def __init__(self, shape, dtype, Op=None, explicit=False,
                 compute=(False, False), todask=(False, False)):
        super().__init__(Op=Op, explicit=explicit)
        self.shape = shape
        self.dtype = dtype
        if Op is None:
            self.Op = None
        self.compute = compute
        self.todask = todask

    def matvec(self, x):
        if self.todask[0]:
            x = da.from_array(x)
        if self.Op is None:
            y = self._matvec(x)
        else:
            y = self.Op._matvec(x)
        y = y.reshape(self.shape[0])
        if self.compute[0]:
            y = y.compute()
        return y

    def rmatvec(self, x):
        if self.todask[1]:
            x = da.from_array(x)
        if self.Op is None:
            y = self._rmatvec(x)
        else:
            y = self.Op._rmatvec(x)
        y = y.reshape(self.shape[1])
        if self.compute[1]:
            y = y.compute()
        return y

    def __call__(self, x):
        return self * x

    def __mul__(self, x):
        return self.dot(x)

    def dot(self, x):
        """Matrix-vector multiplication.

        Parameters
        ----------
        x : :obj:`dask.array.array`
            1-d or 2-d array, representing a vector or matrix.

        Returns
        -------
        Ax : :obj:`dask.array.array`
            1-d or 2-d array (depending on the shape of x) that represents
            the result of applying this linear operator on x.

        """
        if isinstance(x, pLinearOperator):
            return _ProductLinearOperator(self, x)
        elif np.isscalar(x):
            return _ScaledLinearOperator(self, x)
        else:
            if x.ndim == 1 or x.ndim == 2 and x.shape[1] == 1:
                return self.matvec(x)
            elif x.ndim == 2:
                return self.matmat(x)
            else:
                raise ValueError('expected 1-d or 2-d array or matrix, got %r'
                                 % x)

    def __matmul__(self, other):
        if np.isscalar(other):
            raise ValueError("Scalar operands are not allowed, "
                             "use '*' instead")
        return self.__mul__(other)

    def __rmatmul__(self, other):
        if np.isscalar(other):
            raise ValueError("Scalar operands are not allowed, "
                             "use '*' instead")
        return self.__rmul__(other)

    def __rmul__(self, x):
        if np.isscalar(x):
            return _ScaledLinearOperator(self, x)
        else:
            return NotImplemented

    def adjoint(self):
        """Hermitian adjoint.

        Returns the Hermitian adjoint. Can be abbreviated self.H instead
        of self.adjoint().

        """
        return self._adjoint()

    H = property(adjoint)

    def _adjoint(self):
        """Default implementation of _adjoint; defers to rmatvec."""
        shape = (self.shape[1], self.shape[0])
        return _CustomLinearOperator(shape, matvec=self.rmatvec,
                                     rmatvec=self.matvec,
                                     dtype=self.dtype)

    def conj(self):
        """Complex conjugate operator

        Returns
        -------
        eigenvalues : :obj:`pylops.LinearOperator`
            Complex conjugate operator

        """
        return _ConjLinearOperator(self)


class _CustomLinearOperator(LinearOperator):
    """Linear operator defined in terms of user-specified operations."""
    def __init__(self, shape, matvec, rmatvec=None, matmat=None, dtype=None,
                 explicit=None, compute=(False, False), todask=(False, False)):
        super(_CustomLinearOperator, self).__init__(shape=shape,
                                                    dtype=dtype, Op=None,
                                                    explicit=explicit,
                                                    compute=compute,
                                                    todask=todask)
        self.args = ()

        self.__matvec_impl = matvec
        self.__rmatvec_impl = rmatvec
        self.__matmat_impl = matmat

        self._init_dtype()

    def _matmat(self, X):
        if self.__matmat_impl is not None:
            return self.__matmat_impl(X)
        else:
            return super(_CustomLinearOperator, self)._matmat(X)

    def _matvec(self, x):
        return self.__matvec_impl(x)

    def _rmatvec(self, x):
        func = self.__rmatvec_impl
        if func is None:
            raise NotImplementedError("rmatvec is not defined")
        return self.__rmatvec_impl(x)

    def _adjoint(self):
        return _CustomLinearOperator(shape=(self.shape[1], self.shape[0]),
                                     matvec=self.__rmatvec_impl,
                                     rmatvec=self.__matvec_impl,
                                     dtype=self.dtype,
                                     compute=self.compute,
                                     todask=(self.todask[1], self.todask[0]))


class _SumLinearOperator(LinearOperator):
    def __init__(self, A, B):
        if not isinstance(A, pLinearOperator) or \
                not isinstance(B, pLinearOperator):
            raise ValueError('both operands have to be a LinearOperator')
        if A.shape != B.shape:
            raise ValueError('cannot add %r and %r: shape mismatch'
                             % (A, B))
        if A.compute[0] != B.compute[0] or A.compute[1] != B.compute[1]:
            raise ValueError('compute must be the same for A and B')
        self.args = (A, B)
        super(_SumLinearOperator, self).__init__(shape=A.shape,
                                                 dtype=A.dtype, Op=None,
                                                 explicit=A.explicit,
                                                 compute=A.compute,
                                                 todask=A.todask)

    def _matvec(self, x):
        return self.args[0].matvec(x) + self.args[1].matvec(x)

    def _rmatvec(self, x):
        return self.args[0].rmatvec(x) + self.args[1].rmatvec(x)

    def _matmat(self, x):
        return self.args[0].matmat(x) + self.args[1].matmat(x)

    def _adjoint(self):
        A, B = self.args
        return A.H + B.H


class _ProductLinearOperator(LinearOperator):
    def __init__(self, A, B):
        if not isinstance(A, pLinearOperator) or \
                not isinstance(B, pLinearOperator):
            raise ValueError('both operands have to be a LinearOperator')
        if A.shape[1] != B.shape[0]:
            raise ValueError('cannot multiply %r and %r: shape mismatch'
                             % (A, B))
        super(_ProductLinearOperator, self).__init__(shape=(A.shape[0],
                                                            B.shape[1]),
                                                     dtype=A.dtype, Op=None,
                                                     explicit=A.explicit,
                                                     compute=(B.compute[0],
                                                              A.compute[1]),
                                                     todask=(B.todask[0],
                                                             A.todask[1]))
        self.args = (A, B)

    def _matvec(self, x):
        return self.args[0].matvec(self.args[1].matvec(x))

    def _rmatvec(self, x):
        return self.args[1].rmatvec(self.args[0].rmatvec(x))

    def _matmat(self, x):
        return self.args[0].matmat(self.args[1].matmat(x))

    def _adjoint(self):
        A, B = self.args
        return B.H * A.H


class _ScaledLinearOperator(LinearOperator):
    def __init__(self, A, alpha):
        if not isinstance(A, pLinearOperator):
            raise ValueError('LinearOperator expected as A')
        if not np.isscalar(alpha):
            raise ValueError('scalar expected as alpha')
        super(_ScaledLinearOperator, self).__init__(shape=A.shape,
                                                    dtype=A.dtype, Op=None,
                                                    explicit=A.explicit,
                                                    compute=A.compute,
                                                    todask=A.todask)
        self.args = (A, alpha)

    def _matvec(self, x):
        return self.args[1] * self.args[0].matvec(x)

    def _rmatvec(self, x):
        return np.conj(self.args[1]) * self.args[0].rmatvec(x)

    def _matmat(self, x):
        return self.args[1] * self.args[0].matmat(x)

    def _adjoint(self):
        A, alpha = self.args
        return A.H * np.conj(alpha)


class _PowerLinearOperator(LinearOperator):
    def __init__(self, A, p):
        if not isinstance(A, pLinearOperator):
            raise ValueError('LinearOperator expected as A')
        if A.shape[0] != A.shape[1]:
            raise ValueError('square LinearOperator expected, got %r' % A)
        if not np.issubdtype(type(p), int) or p < 0:
            raise ValueError('non-negative integer expected as p')
        super(_PowerLinearOperator, self).__init__(shape=A.shape,
                                                   dtype=A.dtype, Op=None,
                                                   explicit=A.explicit,
                                                   compute=A.compute,
                                                   todask=A.todask)
        A.compute = (False, False)
        self.args = (A, p)

    def _power(self, fun, x, compute):
        res = x.copy()
        for i in range(self.args[1]):
            res = fun(res)
        if compute:
            res = res.compute()
        return res

    def _matvec(self, x):
        return self._power(self.args[0].matvec, x, self.compute[0])

    def _rmatvec(self, x):
        return self._power(self.args[0].rmatvec, x, self.compute[1])

    def _matmat(self, x):
        return self._power(self.args[0].matmat, x, self.compute[0])

    def _adjoint(self):
        A, p = self.args
        return A.H ** p


class _ConjLinearOperator(LinearOperator):
    """Complex conjugate linear operator"""
    def __init__(self, Op):
        if not isinstance(Op, pLinearOperator):
            raise TypeError('Op must be a LinearOperator')
        super(_ConjLinearOperator, self).__init__(shape=Op.shape,
                                                  dtype=Op.dtype, Op=None,
                                                  explicit=Op.explicit,
                                                  compute=Op.compute,
                                                  todask=Op.todask)
        self.oOp = Op # original operator

    def _matvec(self, x):
        x1 = da.conj(x)
        y1 = da.conj(self.oOp._matvec(x1))
        return y1

    def _rmatvec(self, x):
        x1 = da.conj(x)
        y1 = da.conj(self.oOp._rmatvec(x1))
        return y1

    def _adjoint(self):
        return _ConjLinearOperator(self.oOp.H)

