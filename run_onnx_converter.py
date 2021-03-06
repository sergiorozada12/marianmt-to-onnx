from src.onnx import OnnxConverter
from src.config import NAME, MAX_LENGTH, BATCH_SIZE

OUTPUT_PATH = 'onnx/test.onnx'

if __name__ == '__main__':
    converter = OnnxConverter(
        name=NAME,
        batch_size=BATCH_SIZE,
        max_length=MAX_LENGTH,
    )
    
    converter.convert_to_onnx()
    converter.optimize_onnx_model()
    converter.quantize_onnx_model()
    