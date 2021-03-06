import functools
import operator
from copy import deepcopy
import numpy as np

import torch
from transformers import MarianMTModel

import onnx
import onnxruntime
from onnxruntime.quantization import QuantizationMode, quantize

from src.wrappers import MarianDecoderWrapped, MarianDecoderPkvWrapped


class OnnxConverter:
    def __init__(
        self,
        name,
        batch_size,
        max_length,
    ):
        self.model = MarianMTModel.from_pretrained(name)
        self.config = self.model.config
        self.encoder = self.model.model.encoder
        self.decoder = MarianDecoderWrapped(deepcopy(self.model.model.decoder))
        self.decoder_pkv = MarianDecoderPkvWrapped(deepcopy(self.model.model.decoder))

        lm_head_input_size = self.model.lm_head.weight.shape[0]
        lm_head_output_size = self.model.lm_head.weight.shape[1]
        self.lm_head = torch.nn.Linear(lm_head_input_size, lm_head_output_size, bias=True)
        self.lm_head.weight.data = self.model.lm_head.weight
        self.lm_head.bias.data = self.model.final_logits_bias

        self.batch_size = batch_size
        self.max_length = max_length
        self.embedding_size = self.config.d_model

        self.num_decoder_layers = self.config.num_hidden_layers
        self.n_heads = self.config.decoder_attention_heads
        self.d_k = self.embedding_size//self.n_heads

    def _convert_encoder(self):
        encoder_input = torch.randint(10_000, (self.batch_size, self.max_length))
        padding_mask = torch.randint(1, (self.batch_size, self.max_length))

        encoder_inputs = (encoder_input, padding_mask)
        encoder_input_names = ['input_ids', 'attention_mask']
        encoder_output_names = ['output']

        encoder_params_names = encoder_input_names + encoder_output_names
        size_axes = [{0 : 'batch_size', 1: 'seq_length'}]*len(encoder_params_names)
        dynamic_axes = dict(zip(encoder_params_names, size_axes))

        with torch.no_grad():
            encoder_hidden_state = self.encoder(encoder_input, padding_mask, return_dict=False)
            torch.onnx.export(
                self.encoder,
                encoder_inputs,
                "onnx/encoder.onnx",
                export_params=True,
                opset_version=11,
                do_constant_folding=True,
                input_names=encoder_input_names,
                output_names=encoder_output_names,
                dynamic_axes=dynamic_axes)

        onnx_session = onnxruntime.InferenceSession("onnx/encoder.onnx")
        onnx_inputs = dict(zip(encoder_input_names, [arr.numpy() for arr in encoder_inputs]))
        onnx_outputs = onnx_session.run(None, onnx_inputs)

        np.testing.assert_allclose(encoder_hidden_state[0].detach().numpy(), onnx_outputs[0], rtol=1e-03, atol=1e-05)
        print("Encoder exported OK!")

    def _convert_decoder(self):
        decoder_input = torch.randint(10_000, (self.batch_size, self.max_length))
        encoder_hidden_states = torch.rand(self.batch_size, self.max_length, self.embedding_size)
        encoder_mask = torch.randint(1, (self.batch_size, self.max_length))

        decoder_inputs = (decoder_input, encoder_hidden_states, encoder_mask)
        decoder_input_names = ['input_ids','encoder_hidden_states', 'encoder_attention_mask']
        decoder_output_names = ['output']
        decoder_output_names += [f"pkv_{i}" for i in range(24)]

        decoder_params_names = decoder_input_names + decoder_output_names
        size_axes = [{0 : 'batch_size', 1: 'seq_length'}]*4 + [{0 : 'batch_size', 2: 'seq_length'}]*24
        dynamic_axes = dict(zip(decoder_params_names, size_axes))

        with torch.no_grad():
            decoder_hidden_states = self.decoder(*decoder_inputs)
            torch.onnx.export(
                self.decoder,
                decoder_inputs,
                "onnx/decoder.onnx",
                export_params=True,
                opset_version=11,
                do_constant_folding=True,
                input_names=decoder_input_names,
                output_names=decoder_output_names,
                dynamic_axes=dynamic_axes)

        onnx_session = onnxruntime.InferenceSession("onnx/decoder.onnx")
        onnx_inputs = dict(zip(decoder_input_names, [arr.numpy() for arr in decoder_inputs]))
        onnx_outputs = onnx_session.run(None, onnx_inputs)

        np.testing.assert_allclose(decoder_hidden_states[0].detach().numpy(), onnx_outputs[0], rtol=1e-03, atol=1e-05)
        print("Decoder exported OK!")

    def _convert_decoder_pkv(self):
        decoder_input = torch.randint(10_000, (self.batch_size, 1))
        encoder_hidden_states = torch.rand(self.batch_size, self.max_length, self.embedding_size)
        encoder_mask = torch.randint(1, (self.batch_size, self.max_length))

        pkv = torch.ones((self.batch_size, self.n_heads, self.max_length, self.d_k), dtype=torch.float32)
        past_key_values = ((pkv, pkv, pkv, pkv),)*self.num_decoder_layers
        flat_past_key_values = functools.reduce(operator.iconcat, past_key_values, [])
        names_past_key_values = [f"pkv_{i}" for i in range(len(flat_past_key_values))]

        decoder_inputs_raw = [decoder_input, encoder_hidden_states, encoder_mask]
        decoder_inputs = tuple(decoder_inputs_raw + flat_past_key_values)
        decoder_input_names = ['input_ids', 'encoder_hidden_states', 'encoder_attention_mask']
        decoder_input_names += names_past_key_values

        decoder_output_names = ['output']
        for i in range(len(names_past_key_values)):
            if (i//2)%2 != 0:
                name = names_past_key_values[i]
                decoder_output_names.append(name)
            else:
                name = names_past_key_values[i] + 'o'
                decoder_output_names.append(name)
        decoder_param_names = decoder_input_names + decoder_output_names

        dyax_gen = [{0 : 'batch_size', 1: 'seq_length'}]
        dyax_pkv = [{0 : 'batch_size', 2: 'seq_length'}]
        dyax = (
            dyax_gen*len(decoder_inputs_raw) +
            dyax_pkv*len(flat_past_key_values) +
            dyax_gen +
            dyax_pkv*len(flat_past_key_values)
        )
        dynamic_axes = dict(zip(decoder_param_names, dyax))

        with torch.no_grad():
            decoder_hidden_states = self.decoder_pkv(*decoder_inputs)
            torch.onnx.export(
                self.decoder_pkv,
                decoder_inputs,
                "onnx/decoder_pkv.onnx",
                export_params=True,
                opset_version=11,
                do_constant_folding=True,
                input_names=decoder_input_names,
                output_names=decoder_output_names,
                dynamic_axes=dynamic_axes)

        onnx_session = onnxruntime.InferenceSession("onnx/decoder_pkv.onnx")
        onnx_inputs = dict(zip(decoder_input_names, [arr.numpy() for arr in decoder_inputs]))
        onnx_inputs.pop('encoder_hidden_states')
        onnx_outputs = onnx_session.run(None, onnx_inputs)

        np.testing.assert_allclose(decoder_hidden_states[0].detach().numpy(), onnx_outputs[0], rtol=1e-03, atol=1e-05)
        print("Decoder PKV exported OK!")

    def _convert_lm_head(self):
        lm_head_input = torch.rand(self.batch_size, 1, self.embedding_size)
        lm_head_input_name = ['input']
        lm_head_output_name = ['output']

        lm_head_params_name = lm_head_input_name + lm_head_output_name
        size_axes = [{0 : 'batch_size', 1: 'seq_length'}]*len(lm_head_params_name)
        dynamic_axes = dict(zip(lm_head_params_name, size_axes))

        with torch.no_grad():
            lm_head_output = self.lm_head(lm_head_input)
            torch.onnx.export(
                self.lm_head,
                lm_head_input,
                "onnx/lm_head.onnx",
                export_params=True,
                opset_version=11,
                do_constant_folding=True,
                input_names=lm_head_input_name,
                output_names=lm_head_output_name,
                dynamic_axes=dynamic_axes)

        onnx_session = onnxruntime.InferenceSession("onnx/lm_head.onnx")
        onnx_inputs = {'input': lm_head_input.numpy()}
        onnx_outputs = onnx_session.run(None, onnx_inputs)

        np.testing.assert_allclose(lm_head_output.detach().numpy(), onnx_outputs[0], rtol=1e-03, atol=1e-05)
        print("LM Head exported OK!")

    def convert_to_onnx(self):
        self._convert_encoder()
        self._convert_decoder()
        self._convert_decoder_pkv()
        self._convert_lm_head()


    def optimize_onnx_model(self):
        sess_options = onnxruntime.SessionOptions()
        sess_options.graph_optimization_level = onnxruntime.GraphOptimizationLevel.ORT_ENABLE_ALL

        sess_options.optimized_model_filepath = "onnx/encoder.opt.onnx"
        _ = onnxruntime.InferenceSession("onnx/encoder.onnx", sess_options)

        sess_options.optimized_model_filepath = "onnx/decoder.opt.onnx"
        _ = onnxruntime.InferenceSession("onnx/decoder.onnx", sess_options)

        sess_options.optimized_model_filepath = "onnx/decoder_pkv.opt.onnx"
        _ = onnxruntime.InferenceSession("onnx/decoder_pkv.onnx", sess_options)

        sess_options.optimized_model_filepath = "onnx/lm_head.opt.onnx"
        _ = onnxruntime.InferenceSession("onnx/lm_head.onnx", sess_options)
    
    def quantize_onnx_model(self):
        encoder = onnx.load("onnx/encoder.opt.onnx")
        decoder = onnx.load("onnx/decoder.opt.onnx")
        decoder_pkv = onnx.load("onnx/decoder_pkv.opt.onnx")
        lm_head = onnx.load("onnx/lm_head.opt.onnx")

        encoder_quant = quantize(
            model=encoder,
            quantization_mode=QuantizationMode.IntegerOps,
            force_fusions=True,
            symmetric_weight=True,
        )

        decoder_quant = quantize(
            model=decoder,
            quantization_mode=QuantizationMode.IntegerOps,
            force_fusions=True,
            symmetric_weight=True,
        )

        decoder_pkv_quant = quantize(
            model=decoder_pkv,
            quantization_mode=QuantizationMode.IntegerOps,
            force_fusions=True,
            symmetric_weight=True,
        )

        lm_head_quant = quantize(
            model=lm_head,
            quantization_mode=QuantizationMode.IntegerOps,
            force_fusions=True,
            symmetric_weight=True,
        )

        onnx.save_model(encoder_quant, "onnx/encoder.opt.quant.onnx")
        onnx.save_model(decoder_quant, "onnx/decoder.opt.quant.onnx")
        onnx.save_model(decoder_pkv_quant, "onnx/decoder_pkv.opt.quant.onnx")
        onnx.save_model(lm_head_quant, "onnx/lm_head.opt.quant.onnx")
