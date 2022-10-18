# Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
# See https://llvm.org/LICENSE.txt for license information.
# SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
# Also available under a BSD-style license. See LICENSE.
"""Queries the pytorch op registry and generates ODS and CC sources for the ops.
"""

from typing import List, Optional, TextIO

import argparse
import importlib
import logging
import os
import sys

from .utils import TextEmitter
from .registry import Registry, JitOperator

# Mapping from torch types to their corresponding ODS type predicates.
# Use `get_ods_type` instead of using this directly.
TORCH_TYPE_TO_ODS_TYPE = {
    "Tensor": "AnyTorchTensorType",
    "Tensor?": "AnyTorchOptionalTensorType",
    "Tensor?[]": "AnyTorchListOfOptionalTensorType",
    "Tensor[]": "AnyTorchListOfTensorType",
    "Scalar": "AnyTorchScalarType",
    "Scalar?": "AnyTorchOptionalScalarType",
    "int": "Torch_IntType",
    "int[]": "AnyTorchListOfTorchIntType",
    "int?": "AnyTorchOptionalIntType",
    "int[]?": "AnyTorchOptionalListOfTorchIntType",
    "bool": "Torch_BoolType",
    "bool[]": "AnyTorchListOfTorchBoolType",
    "bool?": "AnyTorchOptionalBoolType",
    "float": "Torch_FloatType",
    "float?": "AnyTorchOptionalFloatType",
    "float[]": "AnyTorchListOfTorchFloatType",
    "float[]?": "AnyTorchOptionalListOfTorchFloatType",
    "t[]": "AnyTorchListType",
    "t": "AnyTorchType",
    "t1": "AnyTorchType",
    "t2": "AnyTorchType",
    "Any": "AnyTorchType",
    "Device": "Torch_DeviceType",
    "Device?": "AnyTorchOptionalDeviceType",
    "Generator": "Torch_GeneratorType",
    "Generator?": "AnyTorchOptionalGeneratorType",
    "str": "Torch_StringType",
    "str?": "AnyTorchOptionalStringType",
    "str[]": "AnyTorchListOfTorchStringType",
    "Dict": "Torch_DictType",
    "__torch__.torch.classes.quantized.LinearPackedParamsBase": "Torch_LinearParamsType",
}


def get_ods_type(type: str):
    # TODO: Increase precision on dict type modeling.
    if type.startswith("Dict("):
      type = "Dict"
    ods_type = TORCH_TYPE_TO_ODS_TYPE.get(type)
    if ods_type is None:
        raise Exception(
            f"{type!r} not in TORCH_TYPE_TO_ODS_TYPE mapping. Please add it!")
    return ods_type


def _name_thunk() -> None:
  # Strictly exists for _get_main_module_name to harvest its __module__.
  pass
def _get_main_module_name() -> str:
    # If a Python module is loaded interactively or as part of a module
    # directory, it uses a BuiltinImporter. If loaded from a file, it uses
    # the SourceFileLoader. These two objects have different attributes.
    loader = sys.modules["__main__"].__loader__
    try:
        return loader.name # pytype: disable=attribute-error
    except AttributeError:
        return _name_thunk.__module__

ODS_BANNER = f"""//===-------------------------------------------------------*- tablegen -*-===//
//
// This file is licensed under the Apache License v2.0 with LLVM Exceptions.
// See https://llvm.org/LICENSE.txt for license information.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
// Also available under a BSD-style license. See LICENSE.
//
// Operation summaries and descriptions were systematically derived from public
// API docstrings and are licensed accordingly:
//   https://github.com/pytorch/pytorch/blob/master/LICENSE
//===----------------------------------------------------------------------===//
//
// This file is automatically generated.  Please do not edit.
// Generated via:
// ```
// python -m {_get_main_module_name()}
// ```
//
//===----------------------------------------------------------------------===//


"""


def raw_emit_op(operator: JitOperator,
                emitter_td: TextEmitter,
                *, traits: List[str],
                has_folder: bool, has_canonicalizer: bool):
    """Emit the ODS for a JitOperator to a textual file.

    This is the lowest level of emission and is responsible for low-level
    textual emission details. This function should not have any "smarts"
    for deducing traits/etc.

    You probably don't want to call this directly.
    """
    p_td = lambda *args: emitter_td.print(*args)
    op_name, cpp_class_name = operator.get_mlir_names()

    # Generate unique result names for ops with nameless results
    multiple_results = len(operator.returns) > 1

    def generic_result_name(i):
        return "result" + (str(i) if multiple_results else "")

    p_td(
        f"def Torch_{cpp_class_name} : Torch_Op<{emitter_td.quote(op_name)}, [")
    with emitter_td.indent():
        with emitter_td.indent():
            p_td(",\n".join(traits))
        p_td("]> {")
    with emitter_td.indent():
        summary = f"Generated op for `{operator.unique_key}`"
        p_td(f"let summary = {emitter_td.quote(summary)};")
        p_td(f"let arguments = (ins")
        with emitter_td.indent():
            if operator.is_vararg:
                p_td("Variadic<AnyTorchType>:$operands")
            else:
                p_td(",\n".join([
                    f"""{get_ods_type(arg["type"])}:${arg["name"]}"""
                    for arg in operator.arguments
                ]))
        p_td(");")
        p_td(f"let results = (outs")
        with emitter_td.indent():
            if operator.is_varret:
                p_td("Variadic<AnyTorchType>:$results")
            else:
                p_td(",\n".join([
                    f"""{get_ods_type(ret["type"])}:${ret["name"] or generic_result_name(e)}"""
                    for e, ret in enumerate(operator.returns)
                ]))
        p_td(");")

        if operator.is_vararg or operator.is_varret:
            if operator.is_vararg:
                assembly_operands = "`(` $operands `)`"
                assembly_operand_types = "qualified(type($operands))"
            else:
                assembly_operands = " `,` ".join("$" + arg["name"]
                                                 for arg in operator.arguments)
                assembly_operand_types = " `,` ".join(
                    f"""qualified(type(${arg["name"]}))""" for arg in operator.arguments)
            if operator.is_varret:
                assembly_result_types = "qualified(type($results))"
            else:
                assembly_result_types = " `,` ".join(
                    f"""qualified(type(${ret["name"] or generic_result_name(e)}))"""
                    for e, ret in enumerate(operator.returns))
            if assembly_operand_types and assembly_result_types:
                maybe_arrow = " `->` "
            else:
                maybe_arrow = ""
            assembly_format = f"{assembly_operands} attr-dict `:` {assembly_operand_types}{maybe_arrow}{assembly_result_types}"
            p_td(f"let assemblyFormat = {emitter_td.quote(assembly_format)};")
        else:
            p_td(f"let hasCustomAssemblyFormat = 1;")
            p_td(f"""let extraClassDefinition = [{{
  ParseResult {cpp_class_name}::parse(OpAsmParser &parser, OperationState &result) {{
    return parseDefaultTorchOp(parser, result, {len(operator.arguments)}, {len(operator.returns)});
  }}
  void {cpp_class_name}::print(OpAsmPrinter &printer) {{
    printDefaultTorchOp(printer, *this, {len(operator.arguments)}, {len(operator.returns)});
  }}
}}];
""")
        if has_folder:
            p_td("let hasFolder = 1;")
        if has_canonicalizer:
            p_td("let hasCanonicalizer = 1;")
    p_td("}")
    p_td("\n")


def emit_op(operator: JitOperator,
            emitter_td: TextEmitter,
            *,
            traits: Optional[List[str]] = None,
            has_folder: bool = False,
            has_canonicalizer: bool = False):
    """Main entry point for op emission.

    Besides emitting the op, it deduces / adds traits based on the operator
    information.
    """
    if traits is None:
        traits = []

    # All Torch operators allow type refinement.
    traits += ["AllowsTypeRefinement"]
    if operator.has_value_semantics():
        traits += ["HasValueSemantics"]
    if operator.is_readonly():
        traits += ["ReadOnly"]

    raw_emit_op(operator,
                emitter_td,
                traits=traits,
                has_folder=has_folder,
                has_canonicalizer=has_canonicalizer)


def emit_ops(emitter_td: TextEmitter, registry: Registry):
    def emit(key, **kwargs):
        emit_op(registry[key], emitter_td, **kwargs)

    def emit_with_mutating_variants(key, **kwargs):
        operator = registry[key]
        emit_op(operator, emitter_td, **kwargs)
        ns, unqual, overload = operator.triple
        # Underscore variant of functional ops should have "functional" part removed.
        is_functional_op = overload == "functional"
        emit_op(registry.get_by_triple((ns, unqual + "_", overload if not is_functional_op else "")),
                emitter_td,
                traits=["IsTrailingUnderscoreInplaceVariant"] if not is_functional_op else [])

    # ==========================================================================
    # `aten::` namespace.
    # ==========================================================================

    # Elementwise tensor compute ops
    for key in [
            "aten::tanh : (Tensor) -> (Tensor)",
            "aten::hardtanh : (Tensor, Scalar, Scalar) -> (Tensor)",
            "aten::relu : (Tensor) -> (Tensor)",
            "aten::relu6 : (Tensor) -> (Tensor)",
            "aten::leaky_relu : (Tensor, Scalar) -> (Tensor)",
            "aten::log : (Tensor) -> (Tensor)",
            "aten::sigmoid : (Tensor) -> (Tensor)",
            "aten::hardsigmoid : (Tensor) -> (Tensor)",
            "aten::hardswish : (Tensor) -> (Tensor)",
            "aten::erf : (Tensor) -> (Tensor)",
            "aten::silu : (Tensor) -> (Tensor)",
            "aten::sin : (Tensor) -> (Tensor)",
            "aten::exp : (Tensor) -> (Tensor)",
            "aten::expm1 : (Tensor) -> (Tensor)",
            "aten::cos : (Tensor) -> (Tensor)",
            "aten::atan2 : (Tensor, Tensor) -> (Tensor)",
            "aten::neg : (Tensor) -> (Tensor)",
            "aten::floor : (Tensor) -> (Tensor)",
            "aten::ceil : (Tensor) -> (Tensor)",
            "aten::bitwise_not : (Tensor) -> (Tensor)",
            "aten::div.Tensor : (Tensor, Tensor) -> (Tensor)",
            "aten::logical_or : (Tensor, Tensor) -> (Tensor)",
            "aten::lerp.Tensor : (Tensor, Tensor, Tensor) -> (Tensor)",
            "aten::eq.Tensor : (Tensor, Tensor) -> (Tensor)",
            "aten::gt.Tensor : (Tensor, Tensor) -> (Tensor)",
            "aten::lt.Tensor : (Tensor, Tensor) -> (Tensor)",
            "aten::ne.Tensor : (Tensor, Tensor) -> (Tensor)",
            "aten::div.Scalar : (Tensor, Scalar) -> (Tensor)",
            "aten::ne.Scalar : (Tensor, Scalar) -> (Tensor)",
            "aten::eq.Scalar : (Tensor, Scalar) -> (Tensor)",
            "aten::gt.Scalar : (Tensor, Scalar) -> (Tensor)",
            "aten::ge.Scalar : (Tensor, Scalar) -> (Tensor)",
            "aten::lt.Scalar : (Tensor, Scalar) -> (Tensor)",
            "aten::le.Scalar : (Tensor, Scalar) -> (Tensor)",
            "aten::fmod.Scalar : (Tensor, Scalar) -> (Tensor)",
            "aten::masked_fill.Scalar : (Tensor, Tensor, Scalar) -> (Tensor)",
            "aten::masked_fill.Tensor : (Tensor, Tensor, Tensor) -> (Tensor)",
            "aten::clamp : (Tensor, Scalar?, Scalar?) -> (Tensor)",
            "aten::clamp_min : (Tensor, Scalar) -> (Tensor)",
            "aten::clamp_max : (Tensor, Scalar) -> (Tensor)",
            "aten::log2 : (Tensor) -> (Tensor)",
            "aten::sqrt : (Tensor) -> (Tensor)",
            "aten::log1p : (Tensor) -> (Tensor)",
            "aten::rsqrt : (Tensor) -> (Tensor)",
            "aten::abs : (Tensor) -> (Tensor)",
            "aten::reciprocal : (Tensor) -> (Tensor)",
            "aten::bitwise_and.Tensor : (Tensor, Tensor) -> (Tensor)",
            "aten::bitwise_or.Tensor : (Tensor, Tensor) -> (Tensor)",
            "aten::threshold : (Tensor, Scalar, Scalar) -> (Tensor)",
            "aten::square : (Tensor) -> (Tensor)",
            "aten::unsqueeze : (Tensor, int) -> (Tensor)",
            "aten::zero : (Tensor) -> (Tensor)",
    ]:
        emit_with_mutating_variants(key)
    # Elementwise tensor compute ops that don't have the standard mutating
    # variants.
    emit_with_mutating_variants("aten::div.Tensor_mode : (Tensor, Tensor, str?) -> (Tensor)", has_canonicalizer=True)
    emit_with_mutating_variants("aten::mul.Tensor : (Tensor, Tensor) -> (Tensor)", has_canonicalizer=True)
    emit_with_mutating_variants("aten::add.Tensor : (Tensor, Tensor, Scalar) -> (Tensor)", has_canonicalizer=True)  
    emit_with_mutating_variants("aten::sub.Tensor : (Tensor, Tensor, Scalar) -> (Tensor)", has_canonicalizer=True)  
    emit_with_mutating_variants("aten::add.Scalar : (Tensor, Scalar, Scalar) -> (Tensor)", has_canonicalizer=True)
    emit_with_mutating_variants("aten::sub.Scalar : (Tensor, Scalar, Scalar) -> (Tensor)", has_canonicalizer=True)
    emit_with_mutating_variants("aten::mul.Scalar : (Tensor, Scalar) -> (Tensor)", has_canonicalizer=True)
    
    emit("aten::addcmul : (Tensor, Tensor, Tensor, Scalar) -> (Tensor)")
    emit("aten::addcdiv : (Tensor, Tensor, Tensor, Scalar) -> (Tensor)")
    emit("aten::maximum : (Tensor, Tensor) -> (Tensor)")
    emit("aten::minimum : (Tensor, Tensor) -> (Tensor)")
    emit("aten::mish : (Tensor) -> (Tensor)")
    emit("aten::rsub.Scalar : (Tensor, Scalar, Scalar) -> (Tensor)")
    emit("aten::gelu : (Tensor, str) -> (Tensor)")
    emit("aten::pow.Tensor_Scalar : (Tensor, Scalar) -> (Tensor)")
    emit("aten::pow.Tensor_Tensor : (Tensor, Tensor) -> (Tensor)")
    emit("aten::threshold_backward : (Tensor, Tensor, Scalar) -> (Tensor)")
    emit("aten::floor_divide : (Tensor, Tensor) -> (Tensor)")
    emit("aten::softplus : (Tensor, Scalar, Scalar) -> (Tensor)")

    # Ops without value semantics but the corresponding without trailing
    # underscore variant doesn't exist.
    emit("aten::fill_.Scalar : (Tensor, Scalar) -> (Tensor)")
    emit("aten::uniform_ : (Tensor, float, float, Generator?) -> (Tensor)")
    emit("aten::rand_like : (Tensor, int?, int?, Device?, bool?, int?) -> (Tensor)")
    emit("aten::bernoulli : (Tensor, Generator?) -> (Tensor)")
    emit("aten::bernoulli_.float : (Tensor, float, Generator?) -> (Tensor)")
    emit("aten::bernoulli_.Tensor : (Tensor, Tensor, Generator?) -> (Tensor)")

    emit_with_mutating_variants("aten::triu : (Tensor, int) -> (Tensor)")
    emit_with_mutating_variants("aten::round : (Tensor) -> (Tensor)", has_folder=True)
    emit_with_mutating_variants(
        "aten::index_put : (Tensor, Tensor?[], Tensor, bool) -> (Tensor)")
    emit_with_mutating_variants(
        "aten::index_put.hacked_twin : (Tensor, Tensor[], Tensor, bool) -> (Tensor)")

    # Non-elementwise tensor compute ops
    emit("aten::linear : (Tensor, Tensor, Tensor?) -> (Tensor)")
    emit("aten::mm : (Tensor, Tensor) -> (Tensor)")
    emit("aten::addmm : (Tensor, Tensor, Tensor, Scalar, Scalar) -> (Tensor)")
    emit("aten::matmul : (Tensor, Tensor) -> (Tensor)")
    emit("aten::mv : (Tensor, Tensor) -> (Tensor)")
    emit(
        "aten::conv2d : (Tensor, Tensor, Tensor?, int[], int[], int[], int) -> (Tensor)"
    )
    emit("aten::conv_transpose1d : (Tensor, Tensor, Tensor?, int[], int[], int[], int, int[]) -> (Tensor)")
    emit("aten::conv_transpose2d.input : (Tensor, Tensor, Tensor?, int[], int[], int[], int, int[]) -> (Tensor)")
    emit("aten::conv_transpose3d.input : (Tensor, Tensor, Tensor?, int[], int[], int[], int, int[]) -> (Tensor)")
    emit("aten::convolution : (Tensor, Tensor, Tensor?, int[], int[], int[], bool, int[], int) -> (Tensor)")
    emit("aten::convolution_overrideable : (Tensor, Tensor, Tensor?, int[], int[], int[], bool, int[], int) -> (Tensor)")
    emit("aten::_convolution : (Tensor, Tensor, Tensor?, int[], int[], int[], bool, int[], int, bool, bool, bool, bool) -> (Tensor)")
    emit("aten::_convolution.deprecated : (Tensor, Tensor, Tensor?, int[], int[], int[], bool, int[], int, bool, bool, bool) -> (Tensor)")
    emit("aten::roll : (Tensor, int[], int[]) -> (Tensor)"),
    emit("aten::flip : (Tensor, int[]) -> (Tensor)")
    emit(
        "aten::native_batch_norm : (Tensor, Tensor?, Tensor?, Tensor?, Tensor?, bool, float, float) -> (Tensor, Tensor, Tensor)"
    )
    emit(
        "aten::batch_norm : (Tensor, Tensor?, Tensor?, Tensor?, Tensor?, bool, float, float, bool) -> (Tensor)"
    )
    emit(
        "aten::layer_norm : (Tensor, int[], Tensor?, Tensor?, float, bool) -> (Tensor)"
    )
    emit(
        "aten::native_layer_norm : (Tensor, int[], Tensor?, Tensor?, float) -> (Tensor, Tensor, Tensor)"
    )
    emit(
        "aten::max_pool2d : (Tensor, int[], int[], int[], int[], bool) -> (Tensor)"
    )
    emit(
        "aten::max_pool2d_with_indices : (Tensor, int[], int[], int[], int[], bool) -> (Tensor, Tensor)"
    )
    emit(
        "aten::max_pool2d_with_indices_backward : (Tensor, Tensor, int[], int[], int[], int[], bool, Tensor) -> (Tensor)"
    )
    emit(
        "aten::avg_pool2d : (Tensor, int[], int[], int[], bool, bool, int?) -> (Tensor)"
    )
    emit(
        "aten::softmax.int : (Tensor, int, int?) -> (Tensor)"
    )
    emit(
        "aten::log_softmax.int : (Tensor, int, int?) -> (Tensor)"
    )
    emit(
        "aten::_log_softmax : (Tensor, int, bool) -> (Tensor)"
    )
    emit("aten::adaptive_avg_pool2d : (Tensor, int[]) -> (Tensor)")
    emit("aten::topk : (Tensor, int, int, bool, bool) -> (Tensor, Tensor)")
    emit("aten::transpose.int : (Tensor, int, int) -> (Tensor)")
    emit("aten::permute : (Tensor, int[]) -> (Tensor)")
    emit("aten::bmm : (Tensor, Tensor) -> (Tensor)")
    emit("aten::cumsum : (Tensor, int, int?) -> (Tensor)")
    emit("aten::floor_divide.Scalar : (Tensor, Scalar) -> (Tensor)")
    emit("aten::logsumexp : (Tensor, int[], bool) -> (Tensor)")
    emit("aten::mean.dim : (Tensor, int[]?, bool, int?) -> (Tensor)")
    emit("aten::__and__.Tensor : (Tensor, Tensor) -> (Tensor)")
    emit("aten::_softmax : (Tensor, int, bool) -> (Tensor)")
    emit("aten::mean : (Tensor, int?) -> (Tensor)")
    emit("aten::std : (Tensor, bool) -> (Tensor)")
    emit("aten::std.dim : (Tensor, int[]?, bool, bool) -> (Tensor)")
    emit("aten::var : (Tensor, bool) -> (Tensor)")
    emit("aten::var.dim : (Tensor, int[]?, bool, bool) -> (Tensor)")
    emit("aten::var.correction : (Tensor, int[]?, int?, bool) -> (Tensor)")
    emit("aten::nll_loss_forward : (Tensor, Tensor, Tensor?, int, int) -> (Tensor, Tensor)")
    emit("aten::nll_loss_backward : (Tensor, Tensor, Tensor, Tensor?, int, int, Tensor) -> (Tensor)")
    emit("aten::bincount : (Tensor, Tensor?, int) -> (Tensor)")
    emit("aten::linalg_vector_norm : (Tensor, Scalar, int[]?, bool, int?) -> (Tensor)")
    emit("aten::frobenius_norm.dim : (Tensor, int[], bool) -> (Tensor)")
    emit("aten::mse_loss : (Tensor, Tensor, int) -> (Tensor)")

    # Misc tensor ops.
    emit("aten::constant_pad_nd : (Tensor, int[], Scalar) -> (Tensor)")
    emit("aten::pad : (Tensor, int[], str, float?) -> (Tensor)")
    emit("aten::squeeze.dim : (Tensor, int) -> (Tensor)", has_folder=True)
    emit("aten::squeeze : (Tensor) -> (Tensor)", has_folder=True)
    emit("aten::flatten.using_ints : (Tensor, int, int) -> (Tensor)")
    emit("aten::dim : (Tensor) -> (int)", has_folder=True)
    emit("aten::size : (Tensor) -> (int[])", has_canonicalizer=True)
    emit("aten::Bool.Tensor : (Tensor) -> (bool)")
    emit("aten::is_floating_point : (Tensor) -> (bool)")
    emit("aten::ones : (int[], int?, int?, Device?, bool?) -> (Tensor)")
    emit("aten::new_ones : (Tensor, int[], int?, int?, Device?, bool?) -> (Tensor)")
    emit("aten::zeros : (int[], int?, int?, Device?, bool?) -> (Tensor)")
    emit("aten::new_zeros : (Tensor, int[], int?, int?, Device?, bool?) -> (Tensor)")
    emit("aten::tensor : (t[], int?, Device?, bool) -> (Tensor)")
    emit("aten::tensor.bool : (bool, int?, Device?, bool) -> (Tensor)")
    emit("aten::tensor.int : (int, int?, Device?, bool) -> (Tensor)")
    emit("aten::_shape_as_tensor : (Tensor) -> (Tensor)")
    emit("aten::all : (Tensor) -> (Tensor)")
    emit("aten::all.bool : (bool[]) -> (bool)")
    emit("aten::any : (Tensor) -> (Tensor)")
    emit("aten::any.dim : (Tensor, int, bool) -> (Tensor)")
    emit("aten::arange : (Scalar, int?, int?, Device?, bool?) -> (Tensor)")
    emit("aten::arange.start : (Scalar, Scalar, int?, int?, Device?, bool?) -> (Tensor)")
    emit("aten::arange.start_step : (Scalar, Scalar, Scalar, int?, int?, Device?, bool?) -> (Tensor)")
    emit("aten::arange.start_out : (Scalar, Scalar, Scalar, Tensor) -> (Tensor)")
    emit("aten::argmax : (Tensor, int?, bool) -> (Tensor)")
    emit("aten::bucketize.Tensor : (Tensor, Tensor, bool, bool) -> (Tensor)")
    emit("aten::clone : (Tensor, int?) -> (Tensor)")
    emit("aten::lift_fresh_copy : (Tensor) -> (Tensor)")
    emit("aten::contiguous : (Tensor, int) -> (Tensor)")
    emit("aten::copy_ : (Tensor, Tensor, bool) -> (Tensor)")
    emit("aten::_to_copy : (Tensor, int?, int?, Device?, bool?, bool, int?) -> (Tensor)")
    emit("aten::detach : (Tensor) -> (Tensor)")
    emit("aten::embedding : (Tensor, Tensor, int, bool, bool) -> (Tensor)")
    emit("aten::embedding_bag.padding_idx : (Tensor, Tensor, Tensor, bool, int, bool, Tensor?, bool, int?) -> (Tensor, Tensor, Tensor, Tensor)")
    emit("aten::_embedding_bag : (Tensor, Tensor, Tensor, bool, int, bool, Tensor?, bool, int) -> (Tensor, Tensor, Tensor, Tensor)")
    emit("aten::empty_like : (Tensor, int?, int?, Device?, bool?, int?) -> (Tensor)")
    emit("aten::new_empty : (Tensor, int[], int?, int?, Device?, bool?) -> (Tensor)")
    emit("aten::zeros_like : (Tensor, int?, int?, Device?, bool?, int?) -> (Tensor)")
    emit("aten::ones_like : (Tensor, int?, int?, Device?, bool?, int?) -> (Tensor)")
    emit("aten::empty.memory_format : (int[], int?, int?, Device?, bool?, int?) -> (Tensor)")
    emit("aten::expand : (Tensor, int[], bool) -> (Tensor)")
    emit("aten::expand_as : (Tensor, Tensor) -> (Tensor)")
    emit("aten::broadcast_to : (Tensor, int[]) -> (Tensor)")
    emit("aten::index.Tensor : (Tensor, Tensor?[]) -> (Tensor)")
    emit("aten::index.Tensor_hacked_twin : (Tensor, Tensor[]) -> (Tensor)")
    emit("aten::index_select : (Tensor, int, Tensor) -> (Tensor)")
    emit("aten::_index_put_impl_ : (Tensor, Tensor?[], Tensor, bool, bool) -> (Tensor)")
    emit("aten::item : (Tensor) -> (Scalar)")
    emit("aten::masked_select : (Tensor, Tensor) -> (Tensor)")
    emit("aten::numel : (Tensor) -> (int)")
    emit("aten::repeat : (Tensor, int[]) -> (Tensor)")
    emit("aten::reshape : (Tensor, int[]) -> (Tensor)")
    emit("aten::_reshape_alias : (Tensor, int[], int[]) -> (Tensor)")
    emit("aten::resize_ : (Tensor, int[], int?) -> (Tensor)")
    emit("aten::select.int : (Tensor, int, int) -> (Tensor)")
    emit("aten::size.int : (Tensor, int) -> (int)", has_folder=True)
    emit("aten::stack : (Tensor[], int) -> (Tensor)")
    emit("aten::sum : (Tensor, int?) -> (Tensor)")
    emit("aten::sum.dim_IntList : (Tensor, int[]?, bool, int?) -> (Tensor)")
    emit("aten::max : (Tensor) -> (Tensor)")
    emit("aten::max.dim : (Tensor, int, bool) -> (Tensor, Tensor)")
    emit("aten::to.dtype : (Tensor, int, bool, bool, int?) -> (Tensor)", has_folder=True)
    emit("aten::to.dtype_layout : (Tensor, int?, int?, Device?, bool?, bool, bool, int?) -> (Tensor)", has_folder=True)
    emit("aten::to.other : (Tensor, Tensor, bool, bool, int?) -> (Tensor)")
    emit("aten::to.prim_Device : (Tensor, Device?, int?, bool, bool) -> (Tensor)")
    emit("aten::to.device : (Tensor, Device, int, bool, bool, int?) -> (Tensor)")
    emit("aten::type_as : (Tensor, Tensor) -> (Tensor)", has_folder=True)
    emit("aten::unbind.int : (Tensor, int) -> (Tensor[])")
    emit("aten::view : (Tensor, int[]) -> (Tensor)", has_folder=True)
    emit("aten::_unsafe_view : (Tensor, int[]) -> (Tensor)")
    emit("aten::where.self : (Tensor, Tensor, Tensor) -> (Tensor)")
    emit("aten::where.Scalar : (Tensor, Scalar, Scalar) -> (Tensor)")
    emit("aten::where.ScalarOther : (Tensor, Tensor, Scalar) -> (Tensor)")
    emit("aten::where.ScalarSelf : (Tensor, Scalar, Tensor) -> (Tensor)")
    emit("aten::slice.Tensor : (Tensor, int, int?, int?, int) -> (Tensor)")
    emit("aten::len.Tensor : (Tensor) -> (int)")
    emit("aten::cpu : (Tensor) -> (Tensor)")
    emit("aten::gather : (Tensor, int, Tensor, bool) -> (Tensor)")
    emit("aten::scatter_add : (Tensor, int, Tensor, Tensor) -> (Tensor)")
    emit("aten::IntImplicit : (Tensor) -> (int)")
    emit("aten::FloatImplicit : (Tensor) -> (float)")
    emit("aten::tensor.float : (float, int?, Device?, bool) -> (Tensor)")
    emit("aten::Int.Tensor : (Tensor) -> (int)", has_folder=True)
    emit("aten::Float.Tensor : (Tensor) -> (float)", has_folder=True)
    emit_with_mutating_variants("aten::dropout : (Tensor, float, bool) -> (Tensor)")
    emit("aten::native_dropout : (Tensor, float, bool?) -> (Tensor, Tensor)")
    emit("aten::t : (Tensor) -> (Tensor)")
    emit("aten::numpy_T : (Tensor) -> (Tensor)")
    emit("aten::full : (int[], Scalar, int?, int?, Device?, bool?) -> (Tensor)")
    emit("aten::full_like : (Tensor, Scalar, int?, int?, Device?, bool?, int?) -> (Tensor)")
    emit_with_mutating_variants("aten::baddbmm : (Tensor, Tensor, Tensor, Scalar, Scalar) -> (Tensor)")

    # Functionalization ops
    emit("aten::alias_copy : (Tensor) -> (Tensor)")
    emit("aten::as_strided_copy : (Tensor, int[], int[], int?) -> (Tensor)")
    emit("aten::diagonal_copy : (Tensor, int, int, int) -> (Tensor)")
    emit("aten::expand_copy : (Tensor, int[], bool) -> (Tensor)")
    emit("aten::permute_copy : (Tensor, int[]) -> (Tensor)")
    emit("aten::_reshape_alias_copy : (Tensor, int[], int[]) -> (Tensor)")
    emit("aten::select_copy.int : (Tensor, int, int) -> (Tensor)")
    emit("aten::detach_copy : (Tensor) -> (Tensor)")
    emit("aten::slice_copy.Tensor : (Tensor, int, int?, int?, int) -> (Tensor)")
    emit("aten::squeeze_copy : (Tensor) -> (Tensor)")
    emit("aten::squeeze_copy.dim : (Tensor, int) -> (Tensor)")
    emit("aten::t_copy : (Tensor) -> (Tensor)")
    emit("aten::transpose_copy.int : (Tensor, int, int) -> (Tensor)")
    emit("aten::unsqueeze_copy : (Tensor, int) -> (Tensor)")
    emit("aten::view_copy : (Tensor, int[]) -> (Tensor)")
    emit("aten::view_copy.dtype : (Tensor, int) -> (Tensor)")
    emit("aten::unfold_copy : (Tensor, int, int, int) -> (Tensor)")
    emit("aten::select_scatter : (Tensor, Tensor, int, int) -> (Tensor)")
    emit("aten::slice_scatter : (Tensor, Tensor, int, int?, int?, int) -> (Tensor)")
    emit("aten::diagonal_scatter : (Tensor, Tensor, int, int, int) -> (Tensor)")
    emit("aten::as_strided_scatter : (Tensor, Tensor, int[], int[], int?) -> (Tensor)")
    emit("aten::upsample_nearest2d.vec : (Tensor, int[]?, float[]?) -> (Tensor)")


    # Dict ops.
    emit("aten::__contains__.str : (Dict(str, t), str) -> (bool)", has_folder=True)
    emit("aten::__contains__.int_list : (int[], int) -> (bool)", has_folder=True)
    emit("aten::__getitem__.Dict_str : (Dict(str, t), str) -> (t)", has_folder=True)
    emit("aten::_set_item.str : (Dict(str, t), str, t) -> ()")
    emit("aten::keys.str : (Dict(str, t)) -> (str[])")
    emit("aten::get.default_str : (Dict(str, t), str, t) -> (t)")
    emit("aten::Delete.Dict_str : (Dict(str, t), str) -> ()")

    # List ops.
    emit("aten::cat : (Tensor[], int) -> (Tensor)")
    emit("aten::append.t : (t[], t) -> (t[])")
    emit("aten::add.t : (t[], t[]) -> (t[])", has_canonicalizer=True)
    emit("aten::eq.int_list : (int[], int[]) -> (bool)", has_folder=True)
    emit("aten::list.t : (t[]) -> (t[])")
    emit("aten::slice.t : (t[], int?, int?, int) -> (t[])", has_canonicalizer=True)
    emit("aten::insert.t : (t[], int, t) -> ()")
    emit("aten::ne.int_list : (int[], int[]) -> (bool)")
    emit("aten::any.bool : (bool[]) -> (bool)")

    # Str ops.
    emit("aten::add.str : (str, str) -> (str)")
    emit("aten::eq.str : (str, str) -> (bool)", has_folder=True)
    emit("aten::len.str : (str) -> (int)", has_folder=True)
    emit("aten::str : (t) -> (str)")
    emit("aten::format : (...) -> (str)")
    emit("aten::join : (str, str[]) -> (str)")

    # Type conversion ops.
    emit("aten::Float.Scalar : (Scalar) -> (float)", has_folder=True)
    emit("aten::Float.str : (str) -> (float)")
    emit("aten::Int.float : (float) -> (int)")
    emit("aten::Int.Scalar : (Scalar) -> (int)", has_folder=True)

    # Primitive ops
    emit("aten::__range_length : (int, int, int) -> (int)", has_folder=True)
    emit("aten::__derive_index : (int, int, int) -> (int)", has_folder=True)
    emit("aten::gt.int : (int, int) -> (bool)", has_folder=True)
    emit("aten::ge.int : (int, int) -> (bool)", has_folder=True)
    emit("aten::lt.int : (int, int) -> (bool)", has_folder=True)
    emit("aten::le.int : (int, int) -> (bool)", has_folder=True)
    emit("aten::ne.int : (int, int) -> (bool)", has_folder=True)
    emit("aten::eq.int : (int, int) -> (bool)", has_folder=True)
    emit("aten::floordiv.int : (int, int) -> (int)", has_folder=True)
    emit("aten::remainder.int : (int, int) -> (int)", has_folder=True)
    emit("aten::remainder.Scalar : (Tensor, Scalar) -> (Tensor)")
    emit("aten::add.int : (int, int) -> (int)", has_folder=True)
    emit("aten::sub.int : (int, int) -> (int)", has_folder=True)
    emit("aten::mul.int : (int, int) -> (int)", has_folder=True)
    emit("aten::div.int : (int, int) -> (float)", has_folder=True)
    emit("aten::neg.int : (int) -> (int)", has_folder=True)
    emit("aten::log.int : (int) -> (float)")
    emit("aten::add.float_int : (float, int) -> (float)")
    emit("aten::sub.float : (float, float) -> (float)")
    emit("aten::mul.float : (float, float) -> (float)")
    emit("aten::div.float : (float, float) -> (float)", has_folder=True)
    emit("aten::neg.float : (float) -> (float)")
    emit("aten::eq.float : (float, float) -> (bool)", has_folder=True)
    emit("aten::gt.float : (float, float) -> (bool)", has_folder=True)
    emit("aten::ge.float : (float, float) -> (bool)", has_folder=True)
    emit("aten::lt.float : (float, float) -> (bool)", has_folder=True)
    emit("aten::lt.float_int : (float, int) -> (bool)")
    emit("aten::ge.float_int : (float, int) -> (bool)")
    emit("aten::ne.float_int : (float, int) -> (bool)")
    emit("aten::gt.float_int : (float, int) -> (bool)")
    emit("aten::__and__.bool : (bool, bool) -> (bool)")
    emit("aten::ne.bool : (bool, bool) -> (bool)", has_folder=True)
    emit("aten::__is__ : (t1, t2) -> (bool)", has_folder=True)
    emit("aten::__isnot__ : (t1, t2) -> (bool)", has_folder=True)
    emit("aten::__not__ : (bool) -> (bool)", has_folder=True)
    emit("aten::len.t : (t[]) -> (int)",
         has_folder=True,
         has_canonicalizer=True)
    emit("aten::__getitem__.t : (t[], int) -> (t)", has_canonicalizer=True)
    emit("aten::_set_item.t : (t[], int, t) -> (t[])")
    emit("aten::div : (Scalar, Scalar) -> (float)", has_folder=True)
    emit("aten::add : (Scalar, Scalar) -> (Scalar)")
    emit("aten::sub : (Scalar, Scalar) -> (Scalar)", has_folder=True)
    emit("aten::ceil.Scalar : (Scalar) -> (Scalar)", has_folder=True)
    emit("aten::sqrt.int : (int) -> (float)", has_folder=True)
    emit("aten::Bool.float : (float) -> (bool)", has_folder=True)
    emit("aten::Bool.int : (int) -> (bool)", has_folder=True)

    emit("aten::eq.device : (Device, Device) -> (bool)")
    emit("aten::ceil.float : (float) -> (int)", has_folder=True)
    emit("aten::narrow : (Tensor, int, int, int) -> (Tensor)")
    emit("aten::ScalarImplicit : (Tensor) -> (Scalar)")

    # backprop ops
    emit("aten::_softmax_backward_data : (Tensor, Tensor, int, int) -> (Tensor)")
    emit("aten::tanh_backward : (Tensor, Tensor) -> (Tensor)")
    emit("aten::gelu_backward : (Tensor, Tensor, str) -> (Tensor)")
    emit("aten::_log_softmax_backward_data : (Tensor, Tensor, int, int) -> (Tensor)")
    emit("aten::native_layer_norm_backward : (Tensor, Tensor, int[], Tensor, Tensor, Tensor?, Tensor?, bool[]) -> (Tensor, Tensor, Tensor)")
    emit("aten::embedding_dense_backward : (Tensor, Tensor, int, int, bool) -> (Tensor)")
    emit("aten::native_batch_norm_backward : (Tensor, Tensor, Tensor?, Tensor?, Tensor?, Tensor?, Tensor?, bool, float, bool[]) -> (Tensor, Tensor, Tensor)")
    emit("aten::native_dropout_backward : (Tensor, Tensor, float) -> (Tensor)")

    # ==========================================================================
    # `prim::` namespace.
    # ==========================================================================

    emit("prim::layout : (Tensor) -> (int)")
    emit("prim::TupleIndex : (Any, int) -> (Any)", has_canonicalizer=True)
    emit("prim::device : (Tensor) -> (Device)")
    emit("prim::dtype : (Tensor) -> (int)", has_folder=True)
    emit("prim::TupleUnpack : (Any) -> (...)", has_canonicalizer=True)
    emit("prim::NumToTensor.Scalar : (Scalar) -> (Tensor)")
    emit("prim::min.self_int : (int[]) -> (int)", has_folder=True)
    emit("prim::min.int : (int, int) -> (int)")
    emit("prim::max.self_int : (int[]) -> (int)")
    emit("prim::max.int : (int, int) -> (int)", has_folder=True)
    emit("prim::RaiseException : (str, str?) -> ()")
    emit("prim::Uninitialized : () -> (Any)",
         has_canonicalizer=True, traits=["Pure"])
    emit("prim::unchecked_cast : (t) -> (t)", has_folder=True,
         traits=["DeclareOpInterfaceMethods<CastOpInterface>"])
    emit("prim::Print : (...) -> ()")
    emit("prim::tolist : (...) -> (...)")
    emit("prim::abs.Scalar : (Scalar) -> (Scalar)")

    # ==========================================================================
    # `quantized::` namespace.
    # ==========================================================================

    emit(
        "quantized::linear : (Tensor, __torch__.torch.classes.quantized.LinearPackedParamsBase, float, int) -> (Tensor)",
        traits=["HasValueSemantics"])


def dump_registered_ops(outfile: TextIO, registry: Registry):
    for _, v in sorted(registry.by_unique_key.items()):
        outfile.write(repr(v))

def _maybe_import_op_extensions(args: argparse.Namespace):
    extension_string = str.strip(args.pytorch_op_extensions)
    if len(extension_string) > 0:
        extension_names = extension_string.split(",")
        for name in extension_names:
            # Registration of new PyTorch ops should be a side-effect of
            # importing these modules, so we don't need the return value.
            importlib.import_module(name)

def main(args: argparse.Namespace):
    _maybe_import_op_extensions(args)
    registry = Registry.load()
    if args.debug_registry_dump:
        with open(args.debug_registry_dump, "w") as debug_registry_dump:
            dump_registered_ops(debug_registry_dump, registry)
    td_path = os.path.join(args.torch_ir_include_dir, "GeneratedTorchOps.td")
    with open(td_path, "w") as f_td:
        emitter_td = TextEmitter(f_td)
        emitter_td.print(ODS_BANNER)
        emit_ops(emitter_td, registry)


def _create_argparse() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="generate_ods")
    parser.add_argument(
        "--torch_ir_include_dir",
        required=True,
        help="Directory in include/ containing the Torch dialect")
    parser.add_argument(
        "--debug_registry_dump",
        help="File to dump the the PyTorch JIT operator registry into")
    parser.add_argument(
        "--pytorch_op_extensions",
        type=str,
        default="",
        help="An optional, comma-separated list of Python modules which register additional PyTorch operators upon being imported. These modules can be used to build a torch-mlir which supports PyTorch extensions.")
    return parser


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    parser = _create_argparse()
    args = parser.parse_args()
    main(args)
