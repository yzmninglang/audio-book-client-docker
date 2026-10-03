import os
import base64
import uuid
import io
import argparse
from flask import Flask, request, jsonify
from pydub import AudioSegment

import torch # 确保导入了 torch
import gc    # 导入垃圾回收模块

# 确保我们能正确导入 indextts
import sys
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(current_dir)
sys.path.append(os.path.join(current_dir, "indextts"))

# 从 webui.py 导入核心 TTS 引擎
from indextts.infer_v2 import IndexTTS2



current_dir = os.getcwd()

# 1. 添加 CUDA_PATH
relative_cuda_path = r".\v12.8"  # 使用原始字符串防止转义
absolute_cuda_path = os.path.abspath(os.path.join(current_dir, relative_cuda_path))
os.environ['CUDA_PATH'] = absolute_cuda_path
print(f"已添加临时环境变量 CUDA_PATH: {os.environ['CUDA_PATH']}")

# 2. 添加到 PATH
relative_bin_path = r".\v12.8\bin"  # 使用原始字符串防止转义
absolute_bin_path = os.path.abspath(os.path.join(current_dir, relative_bin_path))

# 获取当前的 PATH 变量
current_path = os.environ.get('PATH', '')

# 判断是否已经存在，避免重复添加
if absolute_bin_path not in current_path:
    # 根据操作系统添加分隔符
    if os.name == 'nt':  # Windows
        os.environ['PATH'] = current_path + os.pathsep + absolute_bin_path
    else:  # Linux, macOS 等
        os.environ['PATH'] = current_path + os.pathsep + absolute_bin_path
    print(f"已添加临时环境变量到 PATH: {absolute_bin_path}")
else:
    print(f"PATH 中已存在: {absolute_bin_path}")

print(f"当前 PATH 变量: {os.environ['PATH']}")




# --- 1. 初始化和参数配置 ---
app = Flask(__name__)

# 使用 argparse 来定义模型目录等配置，与 webui.py 保持一致
parser = argparse.ArgumentParser(description="IndexTTS WebUI Compatible API")
parser.add_argument("--port", type=int, default=8300, help="Port to run the API on")
parser.add_argument("--host", type=str, default="0.0.0.0", help="Host to run the API on")
parser.add_argument("--model_dir", type=str, default="checkpoints", help="Model checkpoints directory")
parser.add_argument("--fp16", action="store_true", default=False, help="Use FP16 for inference")
parser.add_argument("--use_deepspeed", action="store_true", default=False, help="是否开启deepseed加速")
cmd_args = parser.parse_args()


# 定义临时文件目录
TEMP_DIR = "temp_api_files"
os.makedirs(TEMP_DIR, exist_ok=True)

# 启动时加载模型，以提高效率
print(f"正在从 '{cmd_args.model_dir}' 加载 IndexTTS2 模型...")
try:
    # 使用 IndexTTS2，与 webui.py 保持一致
    tts = IndexTTS2(model_dir=cmd_args.model_dir, cfg_path=os.path.join(cmd_args.model_dir, "config.yaml"), use_fp16=cmd_args.fp16, use_cuda_kernel=None,use_deepspeed=cmd_args.use_deepspeed)
    print("模型加载成功！")
except Exception as e:
    print(f"模型加载失败: {e}")
    # 如果模型加载失败，在API调用时会返回错误
    tts = None

def save_base64_to_temp_file(base64_string, prefix="audio"):
    """将Base64编码的音频数据解码并保存到临时文件"""
    if not base64_string:
        return None
    try:
        audio_data = base64.b64decode(base64_string)
        temp_filename = f"{prefix}_{uuid.uuid4()}.wav"
        temp_path = os.path.join(TEMP_DIR, temp_filename)
        with open(temp_path, 'wb') as f:
            f.write(audio_data)
        return temp_path
    except Exception as e:
        # 在主函数中处理这个异常
        raise ValueError(f"Base64解码或保存文件失败: {e}")


# --- 2. 定义扩展后的API接口 ---
@app.route('/api/tts', methods=['POST'])
def synthesize_speech_extended():
    """
    接收POST请求，生成语音并返回包含base64音频的JSON。
    此接口兼容 run_api_book.py 并扩展了 webui.py 的情感控制功能。

    请求体 (JSON):
    {
        "tts_text": "要转换的文本",                             // 必需
        "prompt_audio": "UklGRiT...",                         // 必需, 参考音色的Base64编码音频

        // 可选：情感控制
        "emo_control_method": 1,                              // 0:与音色相同, 1:情感参考音频, 2:情感向量, 3:情感文本
        "emo_audio": "UklGRiQ...",                            // emo_control_method 为 1 时需要
        "emo_weight": 0.8,                                    // emo_control_method 为 1 时可选, 范围 0.0-1.6, 默认 1.0

        "emo_vector": [0.0, 0.5, ...],                        // emo_control_method 为 2 时需要, 8个浮点数的列表
        "emo_text": "高兴",                                   // emo_control_method 为 3 时需要

        // 可选：高级生成参数
        "max_text_tokens_per_sentence": 120,
        "do_sample": true,
        "temperature": 0.8,
        "top_p": 0.8,
        "top_k": 30,
        "num_beams": 3,
        "repetition_penalty": 10.0,
        "length_penalty": 0.0,
        "max_mel_tokens": 1500
    }
    """
    if tts is None:
        return jsonify({"status": "error", "message": "模型未成功加载，无法处理请求"}), 500

    try:
        data = request.get_json()
    except Exception as e:
        return jsonify({"status": "error", "message": f"无效的JSON格式: {e}"}), 400

    if not data:
        return jsonify({"status": "error", "message": "请求体不能为空或不是JSON格式"}), 400

    # --- 3. 提取并映射参数 ---
    text = data.get('tts_text')
    prompt_audio_base64 = data.get('prompt_audio')

    # 校验核心参数
    if not text:
        return jsonify({"status": "error", "message": "参数 'tts_text' 是必需的"}), 400
    if not prompt_audio_base64:
        return jsonify({"status": "error", "message": "参数 'prompt_audio' (音色参考音频) 是必需的"}), 400

    # 情感控制参数
    emo_control_method = data.get('emo_control_method', 0)
    emo_audio_base64 = data.get('emo_audio')
    emo_weight = float(data.get('emo_weight', 1.0))
    emo_vector = data.get('emo_vector')
    emo_text = data.get('emo_text')

    # 高级生成参数 (提供与 webui.py 相同的默认值)
    advanced_params = {
        "max_text_tokens_per_segment": int(data.get("max_text_tokens_per_sentence", 120)),
        "do_sample": bool(data.get("do_sample", True)),
        "top_p": float(data.get("top_p", 0.8)),
        "top_k": int(data.get("top_k", 30)),
        "temperature": float(data.get("temperature", 0.8)),
        "length_penalty": float(data.get("length_penalty", 0.0)),
        "num_beams": int(data.get("num_beams", 3)),
        "repetition_penalty": float(data.get("repetition_penalty", 10.0)),
        "max_mel_tokens": int(data.get("max_mel_tokens", 1500)),
    }

    # --- 4. 处理音频文件和执行TTS ---
    temp_prompt_path = None
    temp_emo_path = None
    output_path = None
    
    try:
        # 解码Base64音频并保存为临时文件
        temp_prompt_path = save_base64_to_temp_file(prompt_audio_base64, "prompt")
        
        # 根据情感控制模式准备参数
        emo_ref_path_for_infer = None
        emo_vector_for_infer = None

        if emo_control_method == 0: # 与音色参考音频相同
            emo_weight = 1.0
        elif emo_control_method == 1: # 使用情感参考音频
            if not emo_audio_base64:
                return jsonify({"status": "error", "message": "当 emo_control_method 为 1 时, 'emo_audio' 是必需的"}), 400
            temp_emo_path = save_base64_to_temp_file(emo_audio_base64, "emo")
            emo_ref_path_for_infer = temp_emo_path
        elif emo_control_method == 2: # 使用情感向量
            if not isinstance(emo_vector, list) or len(emo_vector) != 8:
                return jsonify({"status": "error", "message": "当 emo_control_method 为 2 时, 'emo_vector' 必须是一个包含8个数字的列表"}), 400
            if sum(emo_vector) > 1.5:
                 return jsonify({"status": "error", "message": "情感向量之和不能超过1.5"}), 400
            emo_vector_for_infer = emo_vector
        elif emo_control_method == 3: # 使用情感文本
             if not emo_text:
                return jsonify({"status": "error", "message": "当 emo_control_method 为 3 时, 'emo_text' 是必需的"}), 400
        else: # 默认情况，等同于0
             emo_weight = 1.0


        # 为输出文件生成唯一路径
        output_filename = f"output_{uuid.uuid4()}.wav"
        output_path = os.path.join(TEMP_DIR, output_filename)

        print(f"开始生成语音, 文本: '{text[:50]}...'")
        # 执行TTS推理，传入所有参数
        tts.infer(
            spk_audio_prompt=temp_prompt_path,
            text=text,
            output_path=output_path,
            emo_audio_prompt=emo_ref_path_for_infer,
            emo_alpha=emo_weight,
            emo_vector=emo_vector_for_infer,
            use_emo_text=(emo_control_method == 3),
            emo_text=emo_text,
            verbose=False,
            **advanced_params
        )
        print(f"语音生成完毕: {output_path}")

        # 从生成的路径加载音频文件并进行处理
        audio = AudioSegment.from_wav(output_path)
        
        # 应用一个小的淡出效果以消除可能的爆音
        fade_duration_ms = 30
        audio_faded = audio.fade_out(duration=fade_duration_ms)
        
        sample_rate = audio_faded.frame_rate
        
        # 将处理后的音频导出到内存字节流中
        buffer = io.BytesIO()
        audio_faded.export(buffer, format="wav")
        audio_bytes = buffer.getvalue()
        
        # 将音频编码为Base64字符串
        audio_base64_output = base64.b64encode(audio_bytes).decode('utf-8')

        # 构建成功响应
        response_data = {
            "status": "success",
            "sample_rate": sample_rate,
            "audio": audio_base64_output
        }
        
        return jsonify(response_data), 200

    except ValueError as ve: # 捕捉自定义的解码错误
        return jsonify({"status": "error", "message": str(ve)}), 400
    except Exception as e:
        # 记录详细错误以供调试
        print(f"处理请求时发生未知错误: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"status": "error", "message": f"处理请求时发生内部错误: {e}"}), 500

    finally:
        # --- 5. 清理所有临时文件 ---
        for path in [temp_prompt_path, temp_emo_path, output_path]:
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                    print(f"已删除临时文件: {path}")
                except OSError as e:
                    print(f"删除临时文件失败: {e}")

        # --- 新增：强制垃圾回收和清空CUDA缓存 ---
        # 删除可能占用大量显存的局部变量的引用
        # (虽然Python会自动处理，但在长服务中显式操作更保险)
        temp_vars = [
            'data', 'prompt_audio_base64', 'emo_audio_base64', 'emo_vector',
            'audio', 'audio_faded', 'buffer', 'audio_bytes', 'audio_base64_output'
        ]
        for var_name in temp_vars:
            if var_name in locals():
                del locals()[var_name]

        # 调用Python的垃圾回收器
        gc.collect()
        
        # 清空PyTorch未被使用的CUDA缓存
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            print("已清空CUDA缓存。")

# --- 6. 启动服务 ---
if __name__ == '__main__':
    print('整合包（B站 超级老虎漫步），更多分享：https://space.bilibili.com/86634055?spm_id_from=333.788.0.0 ')
    print("项目地址：https://github.com/index-tts/index-tts")
    # 在生产环境中，建议使用 Gunicorn 或 uWSGI 等 WSGI 服务器
    app.run(host=cmd_args.host, port=cmd_args.port, debug=False)