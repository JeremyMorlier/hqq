#Written by Dr. Hicham Badri @Mobius Labs GmbH - 2023
#####################################################
import torch
import torch.nn.functional as F
import numpy as np 

from .utils    import *
from .optimize import *
from .bitpack  import BitPack 
from tqdm import tqdm
from termcolor import colored

#Main HQQ Quantizer 
class Quantizer:
	SUPPORTED_BITS   = [8, 4, 3, 2]
	optimize_weights = optimize_weights_proximal

	bit_to_packing   = {8:'8bit_u8', 4:'4bit_u8', 3:'3bit_32', 2:'2bit_u8'}

	pack  =  {'8bit_u8':BitPack.pack_8bit_u8,
			  '4bit_u8':BitPack.pack_4bit_u8,
			  '3bit_32':BitPack.pack_3bit_32,
			  '2bit_u8':BitPack.pack_2bit_u8}

	unpack = {'8bit_u8':BitPack.unpack_8bit_u8,
			  '4bit_u8':BitPack.unpack_4bit_u8,
			  '3bit_32':BitPack.unpack_3bit_32,
			  '2bit_u8':BitPack.unpack_2bit_u8}

	@classmethod
	def quantize(cls, tensor, nbits=4, channel_wise=True, group_size=64, optimize=False, round_zero=False, axis=0, bitpack=True):
		assert nbits in Quantizer.SUPPORTED_BITS, "nbits=" + str(nbits) + " not supported."
		assert axis in [0, 1], "axis should be either 0 or 1"
		if(group_size is not None):
			assert is_divisible(tensor.numel(), group_size), "group_size should be divisble by the total tensor dimensions. shape: " + str(tensor.shape) + ", group_size: " + str(group_size)

		W     = tensor.float() 
		shape = W.shape

		#Reshape for grouping
		if((group_size is not None) and channel_wise):
			W = W.reshape([-1, group_size]) if (axis==1) else W.reshape([group_size, -1])
		
		#Get min/max values
		if(channel_wise==False):
			_min, _max = W.min(), W.max()
			optimize   = False
		else:
			_min  = W.min(axis=axis, keepdim=True)[0]
			_max  = W.max(axis=axis, keepdim=True)[0]

		max_v   = 2**nbits - 1 
		min_v   = 0
		min_max = [min_v, max_v]

		#Note: here we work with the inverse of the scale to avoid division and quantize instead via W*scale + zero, the scale is inverted later on.
		scale   = (max_v/(_max - _min)).clamp(max=2e4) #clamp to avoid half-precision problems
		zero    = -_min*scale 

		#Round zero as in: https://github.com/casper-hansen/AutoAWQ/blob/main/awq/quantize/quantizer.py#L42C9-L42C14
		if(round_zero): zero = torch.round(zero)
		
		#Fine-tune weights
		if(optimize): scale, zero = Quantizer.optimize_weights(tensor=W, scale=scale, zero=zero, min_max=min_max, axis=axis)

		#Quantize
		scale, zero = scale.clone(), zero.clone() #Necessary for fake quantization backprop
		W_q  = torch.round(W*scale + zero).clamp(min_max[0], min_max[1])

		#Store meta-data (we invert the scale for dequantization)
		meta = {'nbits':nbits, 'group_size':group_size, 'shape':shape, 'scale':1./scale, 'zero':zero, 'axis':axis, 'packing':Quantizer.bit_to_packing[nbits]}

		#Pack bits
		if(bitpack):
			W_q = Quantizer.pack[meta['packing']](W_q)
		else:
			W_q = W_q.to(tensor.dtype) 
			meta['packing'] = None

		#cleanup
		del W, _min, _max 
		torch.cuda.empty_cache()

		return W_q, meta

	#Main dequantization: bit_unpacking > (W_q - z)*s > reshape
	@classmethod
	def dequantize(cls, W_q, meta):
		compute_dtype = meta['compute_dtype'] if ('compute_dtype' in meta) else torch.float16
		if(meta['packing']):
			W_r = Quantizer.unpack[meta['packing']](W_q).to(compute_dtype)
			if((meta['group_size'] is not None) and (meta['nbits']==3)):
				W_r = W_r[:meta['group_size']] if (meta['axis']==0) else W_r[:,:meta['group_size']]
		else:
			W_r = W_q.to(compute_dtype)
		W_r = ((W_r - meta['zero'])*meta['scale']).reshape(meta['shape']) 
		return W_r

	@classmethod
	def to_inplace(cls, W_q, meta, device):
		compute_dtype = meta['compute_dtype'] if ('compute_dtype' in meta) else torch.float16
		W_q = W_q.to(device).contiguous() 
		for key in meta:
			if(type(meta[key])==torch.Tensor):
				meta[key] = (meta[key].to(compute_dtype) if torch.is_floating_point(meta[key]) else meta[key]).to(device).contiguous() 
		return W_q, meta

	@classmethod
	def to_ooplace(cls, W_q, meta, device):
		compute_dtype = meta['compute_dtype'] if ('compute_dtype' in meta) else torch.float16
		W_q_c  = W_q.to(device).contiguous() 
		meta_c = {}
		for key in meta:
			if(type(meta[key])==torch.Tensor):
				meta_c[key] = (meta[key].to(compute_dtype) if torch.is_floating_point(meta[key]) else meta[key]).to(device).contiguous() 
			else:
				meta_c[key] = meta[key]
		return W_q_c, meta_c

	@classmethod
	def cuda(cls, W_q, meta, device_n=0):
		return Quantizer.to_inplace(W_q, meta, device='cuda:' + str(device_n))

	@classmethod
	def cpu(cls, W_q, meta):
		return Quantizer.to_ooplace(W_q, meta, device='cpu')


#Main linear layer 
try:
	import hqq_aten
	print(colored('hqq_aten package available. Set backend to HQQBackend.ATEN for faster inference and HQQBackend.ATEN_BACKPROP for faster training!', 'cyan'))
except:
	hqq_aten = None

from enum import Enum
class HQQBackend(Enum):
	#Name of the forward functions
	PYTORCH                  = "forward_pytorch" 
	PYTORCH_COMPILE          = "forward_pytorch_compile"
	PYTORCH_BACKPROP         = "forward_pytorch_backprop" 
	PYTORCH_BACKPROP_COMPILE = "forward_pytorch_backprop_compile" 
	ATEN                     = "forward_aten"
	ATEN_BACKPROP            = "forward_aten_backprop"


#No cache: less memory, slower
class HQQMatmulNoCacheDeq(torch.autograd.Function):

	@staticmethod
	def forward(x, dequantize, bias):
		out = torch.matmul(x, dequantize().t())
		if(bias!=None): out += bias
		return out

	@staticmethod
	def setup_context(ctx, inputs, outputs):
		x, dequantize, bias = inputs
		ctx.save_for_backward(x, bias)
		ctx.dequantize = dequantize

	@staticmethod
	def backward(ctx, grad_output):
		x, bias    = ctx.saved_tensors
		dtype_out  = grad_output.dtype

		grad_input = grad_weight = grad_bias = None

		if ctx.needs_input_grad[0]:
			grad_input = torch.matmul(grad_output, ctx.dequantize()) 

		# weight grad for frozen quantized weights not defined
		# if ctx.needs_input_grad[1]:
		# 	grad_weight = torch.matmul(grad_output.t(), x)

		if bias is not None and ctx.needs_input_grad[2]:
			grad_bias = grad_output.sum(0)

		return grad_input, grad_weight, grad_bias


class HQQMatmulNoCacheMul(torch.autograd.Function):

	@staticmethod
	def forward(x, matmul, bias):
		out = matmul(x, transpose=True)
		if(bias!=None): out += bias
		return out

	@staticmethod
	def setup_context(ctx, inputs, outputs):
		x, matmul, bias = inputs
		ctx.save_for_backward(x, bias)
		ctx.matmul = matmul

	@staticmethod
	def backward(ctx, grad_output):
		x, bias    = ctx.saved_tensors

		grad_input = grad_weight = grad_bias = None

		if ctx.needs_input_grad[0]:
			grad_input = ctx.matmul(grad_output, transpose=False)

		# weight grad for frozen quantized weights not defined
		# if ctx.needs_input_grad[1]:
		# 	grad_weight = torch.matmul(grad_output.t(), x)

		if bias is not None and ctx.needs_input_grad[2]:
			grad_bias = grad_output.sum(0)

		return grad_input, grad_weight, grad_bias

class HQQConv2dNoCacheMul(torch.autograd.Function):

	@staticmethod
	def forward(x, conv2d, bias):
		out = conv2d(x)
		if(bias!=None): out += bias
		return out

	@staticmethod
	def setup_context(ctx, inputs, outputs):
		x, matmul, bias = inputs
		ctx.save_for_backward(x, bias)
		ctx.matmul = matmul

	@staticmethod
	def backward(ctx, grad_output):
		x, bias    = ctx.saved_tensors

		grad_input = grad_weight = grad_bias = None

		if ctx.needs_input_grad[0]:
			grad_input = ctx.matmul(grad_output, transpose=False)

		# weight grad for frozen quantized weights not defined
		# if ctx.needs_input_grad[1]:
		# 	grad_weight = torch.matmul(grad_output.t(), x)

		if bias is not None and ctx.needs_input_grad[2]:
			grad_bias = grad_output.sum(0)

		return grad_input, grad_weight, grad_bias
#Cache dequantized tensor: Faster but needs more memory
class HQQMatmulCachedDeq(torch.autograd.Function):

	@staticmethod
	def forward(ctx, x, hqq_layer, bias):
		weight_tmp = hqq_layer.dequantize() 
		out        = torch.matmul(x, weight_tmp.t())
		if(bias!=None): out += bias

		ctx.save_for_backward(x, bias, weight_tmp)
		return out

	@staticmethod
	def backward(ctx, grad_output):
		x, bias, weight_tmp = ctx.saved_tensors

		grad_input = grad_weight = grad_bias = None

		if ctx.needs_input_grad[0]:
			grad_input = torch.matmul(grad_output, weight_tmp) 

		del weight_tmp

		if bias is not None and ctx.needs_input_grad[2]:
			grad_bias = grad_output.sum(0)

		return grad_input, grad_weight, grad_bias

#Main linear layer 
class HQQLinear(torch.nn.Module):
	backend = HQQBackend.PYTORCH #Default

	def __init__(self, linear_layer, quant_config, del_orig=True, compute_dtype=torch.float16, device_n=0):
		super().__init__()
		self.ready         = False
		self.in_gpu        = False
		self.device        = None
		self.bias          = None
		self.device_n      = device_n
		self.compute_dtype = compute_dtype
		self.quant_config  = quant_config
		self.set_backend(HQQLinear.backend) #Default backend

		if(linear_layer is not None):
			self.bias = None if (linear_layer.bias==None) else linear_layer.bias.to(self.compute_dtype).cuda()
			self.quantize(linear_layer.weight.data, **quant_config)

		if(del_orig): del linear_layer
		torch.cuda.empty_cache()

	#Set backends
	@classmethod
	def set_backend(cls, backend: HQQBackend):
		HQQLinear.backend = backend
		cls.forward       = getattr(cls, backend.value)

	def cuda(self, device_n=0):
		if(self.in_gpu): return 
		self.meta['compute_dtype'] = self.compute_dtype 
		self.W_q, self.meta = Quantizer.cuda(self.W_q, self.meta, device_n)
		if(self.meta['quant_scale']):
			self.meta['scale_q'] , self.meta['meta_scale'] = Quantizer.cuda(self.meta['scale_q'], self.meta['meta_scale'], device_n)
		if(self.meta['quant_zero']):
			self.meta['zero_q'] , self.meta['meta_zero']   = Quantizer.cuda(self.meta['zero_q'], self.meta['meta_zero'], device_n)

		if(self.bias is not None):
			self.bias = self.bias.to(self.compute_dtype).cuda(device_n)

		self.W_q    = torch.nn.Parameter(self.W_q, requires_grad=False)
		self.device = self.W_q.device
		self.in_gpu = True

	def to(self, *args, **kwargs):
		pass

	def half(self, *args, **kwargs):
		return self 

	def state_dict(self):
		return {'W_q':self.W_q, 'meta':self.meta, 'bias':self.bias}

	def load_state_dict(self, state_dict):
		self.W_q    = state_dict['W_q']
		self.meta   = state_dict['meta']
		self.bias   = state_dict['bias'] if ('bias' in state_dict) else None
		self.in_gpu = self.W_q.device.type == 'cuda'
		if(self.in_gpu): 
			if('scale' in self.meta):
				self.meta['scale'] = self.meta['scale'].to(self.compute_dtype)
			if('zero' in self.meta):
				self.meta['zero']  = self.meta['zero'].to(self.compute_dtype)
		else:
			self.cuda(self.device_n)
		self.ready  = True

	#@torch.inference_mode()
	def quantize(self, W, weight_quant_params, scale_quant_params, zero_quant_params):
		quant_scale = scale_quant_params is not None
		quant_zero  = zero_quant_params  is not None

		self.in_features, self.out_features = W.t().shape
		
		#Quantize
		W_q , meta = Quantizer.quantize(W, **weight_quant_params) 
		meta.update({'quant_scale':quant_scale, 'quant_zero':quant_zero})
		if(meta['quant_scale']):
			meta['scale_q'] , meta['meta_scale'] = Quantizer.quantize(meta['scale'], **scale_quant_params); del meta['scale']
			meta['meta_scale']['compute_dtype']  = self.compute_dtype
		if(meta['quant_zero']):
			meta['zero_q'], meta['meta_zero']    = Quantizer.quantize(meta['zero'],  **zero_quant_params);  del meta['zero']
			meta['meta_zero']['compute_dtype']   = self.compute_dtype

		self.W_q   = W_q
		self.meta  = meta 
		self.cuda(self.device_n)
		self.ready = True

	def dequantize(self):
		assert self.ready, "model was not quantized"
		W_q, meta = self.W_q, self.meta

		del_keys = []
		if(meta['quant_scale']):
			meta['scale'] = Quantizer.dequantize(meta['scale_q'], meta['meta_scale']); del_keys.append('scale')
		if(meta['quant_zero']):
			meta['zero']  = Quantizer.dequantize(meta['zero_q'],  meta['meta_zero']);  del_keys.append('zero')

		W_est = Quantizer.dequantize(W_q, meta)

		#Cleanup
		for key in del_keys: del meta[key]
		return W_est

	def matmul(self, x, transpose=True):
		weight = self.dequantize() 
		return torch.matmul(x, weight.t() if (transpose) else weight)

	@torch.compile()
	def matmul_compile(self, *args, **kwargs):
		return self.matmul(*args, **kwargs)

	def forward_pytorch_backprop(self, x):
		return HQQMatmulNoCacheMul.apply(x, self.matmul, self.bias)

	def forward_pytorch_backprop_compile(self, x):
		return HQQMatmulNoCacheMul.apply(x, self.matmul_compile, self.bias)

	def forward_pytorch(self, x): 
		out = torch.matmul(x, self.dequantize().t())
		if(self.bias is not None):
			out += self.bias
		return out

	@torch.compile()
	def forward_pytorch_compile(self, x): 
		return self.forward_pytorch(x)

	##############################################
	#Experimental
	#############################################
	#Requires building the aten backend
	@torch.jit.ignore
	def dequantize_Wq_aten(self, W_q, meta):
		return hqq_aten.dequantize(W_q, meta['scale'], meta['zero'], meta['shape'], meta['group_size'] if (meta['group_size']) else -1, meta['nbits'], meta['axis'], meta['packing'])

	def dequantize_aten(self):
		#Dequantize 
		assert self.ready, "model was not quantized"
		W_q, meta = self.W_q, self.meta

		del_keys = []
		if(meta['quant_scale']):
			if(meta['meta_scale']['group_size']):
				meta['scale'] = self.dequantize_Wq_aten(meta['scale_q'], meta['meta_scale']); del_keys.append('scale')
			else:
				meta['scale'] = Quantizer.dequantize(meta['scale_q'], meta['meta_scale']); del_keys.append('scale')
		if(meta['quant_zero']):
			if(meta['meta_zero']['group_size']):
				meta['zero'] = self.dequantize_Wq_aten(meta['zero_q'], meta['meta_zero']); del_keys.append('zero')
			else:
				meta['zero']  = Quantizer.dequantize(meta['zero_q'],  meta['meta_zero']);  del_keys.append('zero')
		
		W_est = self.dequantize_Wq_aten(W_q, meta)

		#Cleanup
		for key in del_keys: del meta[key]

		return W_est

	def forward_aten(self, x):
		W_est = self.dequantize_aten()
		out   = torch.matmul(x, W_est.t())
		if(self.bias is not None):
			out += self.bias
		return out

	def forward_aten_backprop(self, x):
		return HQQMatmulNoCacheDeq.apply(x, self.dequantize_aten, self.bias)


#Main Conv2d layer 
class HQQConv2d(torch.nn.Module):
	backend = HQQBackend.PYTORCH #Default

	def __init__(self, conv2d_layer, quant_config, del_orig=True, compute_dtype=torch.float16, device_n=0):
		super().__init__()
		self.ready         = False
		self.in_gpu        = False
		self.device        = None
		self.bias          = None
		self.device_n      = device_n
		self.compute_dtype = compute_dtype
		self.quant_config  = quant_config
		self.set_backend(HQQLinear.backend) #Default backend

		if(conv2d_layer is not None):
			self.bias = None if (conv2d_layer.bias==None) else conv2d_layer.bias.to(self.compute_dtype).cuda()
			self.in_channels, self.out_channels = conv2d_layer.in_channels, conv2d_layer.out_channels
			self.kernel_size = conv2d_layer.kernel_size
			self.stride, self.padding, self.dilation, self.groups = conv2d_layer.stride, conv2d_layer.padding, conv2d_layer.dilation, conv2d_layer.groups
			self.quantize(conv2d_layer.weight.data, **quant_config)

		if(del_orig): del conv2d_layer
		torch.cuda.empty_cache()

	def extra_repr(self):
		s = f'{self.in_channels},  {self.out_channels}, kernel_size={self.kernel_size}, stride={self.stride}'
		if self.padding != (0,) * len(self.padding):
			s += f', padding={self.padding}'
		if self.dilation != (1,) * len(self.dilation):
			s += f', dilation={self.dilation}'
		if self.groups != 1:
			s += f', groups={self.groups}'
		if self.bias is None:
			s += f', bias=False'
		return s

	#Set backends
	@classmethod
	def set_backend(cls, backend: HQQBackend):
		HQQLinear.backend = backend
		cls.forward       = getattr(cls, backend.value)

	def cuda(self, device_n=0):
		if(self.in_gpu): return 
		self.meta['compute_dtype'] = self.compute_dtype 
		self.W_q, self.meta = Quantizer.cuda(self.W_q, self.meta, device_n)
		if(self.meta['quant_scale']):
			self.meta['scale_q'] , self.meta['meta_scale'] = Quantizer.cuda(self.meta['scale_q'], self.meta['meta_scale'], device_n)
		if(self.meta['quant_zero']):
			self.meta['zero_q'] , self.meta['meta_zero']   = Quantizer.cuda(self.meta['zero_q'], self.meta['meta_zero'], device_n)

		if(self.bias is not None):
			self.bias = self.bias.to(self.compute_dtype).cuda(device_n)

		self.W_q    = torch.nn.Parameter(self.W_q, requires_grad=False)
		self.device = self.W_q.device
		self.in_gpu = True

	def to(self, *args, **kwargs):
		pass

	def half(self, *args, **kwargs):
		return self 

	def state_dict(self):
		return {'W_q':self.W_q, 'meta':self.meta, 'bias':self.bias}

	def load_state_dict(self, state_dict):
		self.W_q    = state_dict['W_q']
		self.meta   = state_dict['meta']
		self.bias   = state_dict['bias'] if ('bias' in state_dict) else None
		self.in_gpu = self.W_q.device.type == 'cuda'
		if(self.in_gpu): 
			if('scale' in self.meta):
				self.meta['scale'] = self.meta['scale'].to(self.compute_dtype)
			if('zero' in self.meta):
				self.meta['zero']  = self.meta['zero'].to(self.compute_dtype)
		else:
			self.cuda(self.device_n)
		self.ready  = True

	#@torch.inference_mode()
	def quantize(self, W, weight_quant_params, scale_quant_params, zero_quant_params):
		quant_scale = scale_quant_params is not None
		quant_zero  = zero_quant_params  is not None

		#self.in_features, self.out_features = W.t().shape
		
		#Quantize
		W_q , meta = Quantizer.quantize(W, **weight_quant_params) 
		meta.update({'quant_scale':quant_scale, 'quant_zero':quant_zero})
		if(meta['quant_scale']):
			meta['scale_q'] , meta['meta_scale'] = Quantizer.quantize(meta['scale'], **scale_quant_params); del meta['scale']
			meta['meta_scale']['compute_dtype']  = self.compute_dtype
		if(meta['quant_zero']):
			meta['zero_q'], meta['meta_zero']    = Quantizer.quantize(meta['zero'],  **zero_quant_params);  del meta['zero']
			meta['meta_zero']['compute_dtype']   = self.compute_dtype

		self.W_q   = W_q
		self.meta  = meta 
		self.cuda(self.device_n)
		self.ready = True

	def dequantize(self):
		assert self.ready, "model was not quantized"
		W_q, meta = self.W_q, self.meta

		del_keys = []
		if(meta['quant_scale']):
			meta['scale'] = Quantizer.dequantize(meta['scale_q'], meta['meta_scale']); del_keys.append('scale')
		if(meta['quant_zero']):
			meta['zero']  = Quantizer.dequantize(meta['zero_q'],  meta['meta_zero']);  del_keys.append('zero')

		W_est = Quantizer.dequantize(W_q, meta)

		#Cleanup
		for key in del_keys: del meta[key]
		return W_est

	def conv2d(self, x, transpose=True):
		weight = self.dequantize() 
		return F.conv2d(x, weight, bias=None, stride=self.stride, padding=self.padding, dilation=self.dilation, groups=self.groups)

	@torch.compile()
	def conv2d_compile(self, *args, **kwargs):
		return self.conv2d(*args, **kwargs)

	def forward_pytorch_backprop(self, x):
		return HQQConv2dNoCacheMul.apply(x, self.conv2d, self.bias)

	def forward_pytorch_backprop_compile(self, x):
		return HQQConv2dNoCacheMul.apply(x, self.conv2d_compile, self.bias)

	def forward_pytorch(self, x): 
		out = torch.conv2d(x, self.dequantize(), bias=None, stride=self.stride, padding=self.padding, dilation=self.dilation, groups=self.groups)
		if(self.bias is not None):
			out += self.bias
		return out

	@torch.compile()
	def forward_pytorch_compile(self, x): 
		return self.forward_pytorch(x)


def hqq_base_quant_config(nbits=4, group_size=64, quant_zero=True, quant_scale=False):
	assert nbits in Quantizer.SUPPORTED_BITS, "nbits value not supported. Check Quantizer.SUPPORTED_BITS."
	if(group_size is not None):
		assert is_divisible(group_size, 8), "Invalid group_size param: the value should be a multiple of 8." 
	weight_quant_params = {'nbits':nbits,'channel_wise':True,  'group_size':group_size, 'optimize':True, 'round_zero':True if nbits==4 else False} 
	scale_quant_params  = {'nbits':8,    'channel_wise':True,  'group_size':128,        'optimize':False} if (quant_scale) else None
	zero_quant_params   = {'nbits':8,    'channel_wise':False, 'group_size':None,       'optimize':False} if (quant_zero)  else None
	return {'weight_quant_params':weight_quant_params, 'scale_quant_params':scale_quant_params, 'zero_quant_params':zero_quant_params}

#Alias: follow similar Auto-GPTQ naming
BaseQuantizeConfig = hqq_base_quant_config