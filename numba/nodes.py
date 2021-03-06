import ast
import collections

import numba
from .symtab import Variable
from . import _numba_types as numba_types
from numba import utils
from numba.minivect import minitypes

import llvm.core

from ndarray_helpers import PyArrayAccessor

context = utils.get_minivect_context()


def _const_int(X):
    return llvm.core.Constant.int(llvm.core.Type.int(), X)

class Node(ast.AST):
    """
    Superclass for Numba AST nodes
    """
    _fields = []

    def __init__(self, **kwargs):
        vars(self).update(kwargs)

class CoercionNode(Node):
    _fields = ['node']
    def __init__(self, node, dst_type):
        self.node = node
        self.dst_type = dst_type
        self.variable = Variable(dst_type)
        self.type = dst_type

    @classmethod
    def coerce(cls, node_or_nodes, dst_type):
        if isinstance(node_or_nodes, list):
            return [cls(node, dst_type) for node in node_or_nodes]
        return cls(node_or_nodes, dst_type)

class DeferredCoercionNode(CoercionNode):
    """
    Coerce to the type of the given variable. The type of the variable may
    change in the meantime (e.g. may be promoted or demoted).
    """

    _fields = ['node']

    def __init__(self, node, variable):
        self.node = node
        self.variable = variable

class ConstNode(Node):
    def __init__(self, pyval, type=None):
        if type is None:
            type = context.typemapper.from_python(pyval)

        self.variable = Variable(type, is_constant=True, constant_value=pyval)
        self.type = type
        self.pyval = pyval

    def value(self, builder):
        type = self.type
        ltype = type.to_llvm(context)

        constant = self.pyval

        if type.is_float:
            lvalue = llvm.core.Constant.real(ltype, constant)
        elif type.is_int:
            lvalue = llvm.core.Constant.int(ltype, constant)
        elif type.is_complex:
            base_ltype = self.to_llvm(type.base_type)
            lvalue = llvm.core.Constant.struct([(base_ltype, constant.real),
                                                (base_ltype, constant.imag)])
        elif type.is_pointer and self.pyval == 0:
            return llvm.core.ConstantPointerNull
        elif type.is_object:
            raise NotImplementedError
        elif type.is_function:
            # TODO:
            # lvalue = map_to_function(constant, type, self.mod)
            raise NotImplementedError
        else:
            raise NotImplementedError("Constant %s of type %s" %
                                                        (self.pyval, type))

        return lvalue

class FunctionCallNode(Node):
    def __init__(self, signature, args):
        self.signature = signature
        self.args = [CoercionNode(arg, arg_dst_type)
                         for arg_dst_type, arg in zip(signature.args, args)]
        self.variable = Variable(signature.return_type)

class NativeCallNode(FunctionCallNode):
    _fields = ['args']

    def __init__(self, signature, args, llvm_func, py_func=None):
        super(NativeCallNode, self).__init__(signature, args)
        self.llvm_func = llvm_func
        self.py_func = py_func

class ObjectCallNode(FunctionCallNode):
    _fields = ['function', 'args', 'kwargs']

    def __init__(self, signature, call_node, py_func=None):
        super(ObjectCallNode, self).__init__(signature, call_node.args)
        self.function = call_node.func
        if call_node.keywords:
            keywords = [(k.arg, k.value) for k in call_node.keywords]
            keys, values = zip(*keywords)
            self.kwargs = ast.Dict(keys, values)
            self.kwargs.variable = Variable(minitypes.object_)
        else:
            self.kwargs = ConstNode(0, minitypes.object_.pointer())
        self.py_func = py_func

class ObjectTempNode(Node):
    """
    Coerce a node to a temporary which is reference counted.
    """

    _fields = ['node']

    def __init__(self, node):
        self.node = node
        self.llvm_temp = None

class TempNode(Node): #, ast.Name):
    """
    Create a temporary to store values in. Does not perform reference counting.
    """

    temp_counter = 0

    def __init__(self, type):
        self.type = type
        self.variable = Variable(type, name='___numba_%d' % self.temp_counter,
                                 is_local=True)
        TempNode.temp_counter += 1
        self.llvm_temp = None

    def load(self):
        return TempLoadNode(temp=self)

    def store(self):
        return TempStoreNode(temp=self)

class TempLoadNode(Node):
    _fields = ['temp']

class TempStoreNode(Node):
    _fields = ['temp']

# This appraoch is wrong. This way we cannot visit the original node, and the
# numpy array must be a name. What if I have call function that returns a numpy
# array and I index it? Separate the subscript logic below into the code
# generator, and have DataPointerNode's only return the data pointer.
class DataPointerNode(Node):

    _fields = ['node']

    def __init__(self, node):
        self.node = node
        self.variable = Variable(node.type)
        self.type = node.type

    @property
    def ndim(self):
        return self.variable.type.ndim

    def data_descriptors(self, builder):
        '''
        Returns a tuple of (dptr, strides)
        - dptr:    a pointer of the data buffer
        - strides: a pointer to an array of stride information;
                   has `ndim` elements.
        '''
        pyarray_ptr = builder.load(self.variable.lvalue)
        acc = PyArrayAccessor(builder, pyarray_ptr)
        return acc.data, acc.strides

    def subscript(self, translator, indices):
        builder = translator.builder
        caster = translator.caster
        context = translator.context

        dptr, strides = self.data_descriptors(builder)
        ndim = self.ndim

        offset = _const_int(0)

        if not isinstance(indices, collections.Iterable):
            indices = (indices,)

        for i, index in zip(range(ndim), reversed(indices)):
            # why is the indices reversed?
            stride_ptr = builder.gep(strides, [_const_int(i)])
            stride = builder.load(stride_ptr)
            index = caster.cast(index, stride.type)
            offset = caster.cast(offset, stride.type)
            offset = builder.add(offset, builder.mul(index, stride))

        data_ty = self.variable.type.dtype.to_llvm(context)
        data_ptr_ty = llvm.core.Type.pointer(data_ty)

        dptr_plus_offset = builder.gep(dptr, [offset])

        ptr = builder.bitcast(dptr_plus_offset, data_ptr_ty)
        return ptr


class ArrayAttributeNode(Node):
    is_read_only = True

    _fields = ['array']

    def __init__(self, attribute_name, array):
        self.array = array
        self.attr_name = attribute_name

        array_type = array.variable.type
        if attribute_name == 'ndim':
            type = minitypes.int_
        elif attribute_name in ('shape', 'strides'):
            type = minitypes.CArrayType(numba_types.intp, array_type.ndim)
        elif attribute_name == 'data':
            type = array_type.dtype.pointer()
        else:
            raise NotImplementedError(node.attr)

        self.type = type
        self.variable = Variable(type)

class ShapeAttributeNode(ArrayAttributeNode):
    # NOTE: better do this at code generation time, and not depend on
    #       variable.lvalue
    _fields = ['array']

    def __init__(self, array):
        super(ShapeAttributeNode, self).__init__('shape', array)
        self.array = array
        self.element_type = numba_types.intp
        self.type = minitypes.CArrayType(self.element_type,
                                         array.variable.type.ndim)
        self.variable = Variable(self.type)

