import asyncio
import os
import re
import tempfile
import zipfile
import edge_tts
import uvicorn
import shutil
import subprocess


# ============================================================
# 项目目录
# ============================================================

BASE_DIR = os.path.dirname(
    os.path.abspath(__file__)
)

WEB_DIR = os.path.join(BASE_DIR, "web")
TXT_DIR = os.path.join(BASE_DIR, "txt")
AUDIO_DIR = os.path.join(BASE_DIR, "audio")
SUBTITLE_DIR = os.path.join(BASE_DIR, "subtitles")
ZIP_DIR = os.path.join(BASE_DIR, "zip")

os.makedirs(WEB_DIR, exist_ok=True)
os.makedirs(TXT_DIR, exist_ok=True)
os.makedirs(AUDIO_DIR, exist_ok=True)
os.makedirs(SUBTITLE_DIR, exist_ok=True)
os.makedirs(ZIP_DIR, exist_ok=True)


from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


# ============================================================
# FastAPI
# ============================================================

app = FastAPI(title="Edge TTS Service")


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# Voice
# ============================================================

VOICE_MAP = {
    "1": (
        "de-DE-SeraphinaMultilingualNeural",
        "Seraphina (多语言女声)",
        "Seraphina"
    ),
    "2": (
        "de-DE-KatjaNeural",
        "Katja (经典德语女声)",
        "Katja"
    ),
    "3": (
        "de-DE-AmalaNeural",
        "Amala (温柔德语女声)",
        "Amala"
    ),
    "4": (
        "de-DE-ConradNeural",
        "Conrad (标准德语男声)",
        "Conrad"
    ),
    "5": (
        "de-DE-KillianNeural",
        "Killian (沉稳德语男声)",
        "Killian"
    ),
    "6": (
        "de-DE-FlorianMultilingualNeural",
        "Florian (多语言男声)",
        "Florian"
    ),
}


# ============================================================
# 根据 Voice 名字直接寻找 Voice
#
# 例如：
#
# Amala
# ↓
# de-DE-AmalaNeural
#
# Seraphina
# ↓
# de-DE-SeraphinaMultilingualNeural
# ============================================================

def get_voice_by_tag(
    speaker_name
):

    speaker_name = (
        speaker_name
        .strip()
        .lower()
    )

    for key, (
        voice_id,
        voice_name,
        voice_tag
    ) in VOICE_MAP.items():

        if (
            voice_tag.lower()
            ==
            speaker_name
        ):

            return voice_id

    return None


# ============================================================
# 文件名
# ============================================================

def safe_filename(name):

    name = name.strip()

    if not name:
        name = "article"

    return re.sub(
        r'[<>:"/\\|?*]',
        "_",
        name
    )


# ============================================================
# SRT 时间
# ============================================================

def format_srt_time(seconds):

    seconds = max(
        0,
        float(seconds)
    )

    total_ms = int(
        round(seconds * 1000)
    )

    hours = total_ms // 3600000
    total_ms %= 3600000

    minutes = total_ms // 60000
    total_ms %= 60000

    secs = total_ms // 1000
    millis = total_ms % 1000

    return (
        f"{hours:02d}:"
        f"{minutes:02d}:"
        f"{secs:02d},"
        f"{millis:03d}"
    )


# ============================================================
# 原始 TTS + SentenceBoundary
# ============================================================

async def generate_audio_with_boundaries(
    text,
    voice_id,
    output_mp3,
    timeout=60
):

    retry_count = 0

    while True:

        retry_count += 1

        print()
        print("=" * 70)
        print(
            f"[TTS] 第 {retry_count} 次尝试"
        )
        print("=" * 70)

        if os.path.exists(output_mp3):

            try:
                os.remove(output_mp3)
            except:
                pass

        boundaries = []

        communicate = edge_tts.Communicate(
            text,
            voice_id
        )

        async def consume():

            with open(
                output_mp3,
                "wb"
            ) as f:

                async for chunk in communicate.stream():

                    if chunk["type"] == "audio":

                        f.write(
                            chunk["data"]
                        )

                    elif chunk["type"] == "SentenceBoundary":

                        boundaries.append({
                            "offset": chunk["offset"],
                            "duration": chunk["duration"],
                            "text": chunk["text"]
                        })

        try:

            await asyncio.wait_for(
                consume(),
                timeout=timeout
            )

            if (
                os.path.exists(output_mp3)
                and
                os.path.getsize(output_mp3) > 0
                and
                boundaries
            ):

                print(
                    f"[TTS] 成功: "
                    f"{os.path.getsize(output_mp3)} bytes"
                )

                print(
                    f"[TTS] SentenceBoundary: "
                    f"{len(boundaries)} 个"
                )

                return True, boundaries

            raise Exception(
                "没有获取到 SentenceBoundary"
            )

        except Exception as e:

            print(
                f"[TTS ERROR] {e}"
            )

            if os.path.exists(output_mp3):

                try:
                    os.remove(output_mp3)
                except:
                    pass

        print(
            "[TTS] 3 秒后重试..."
        )

        await asyncio.sleep(3)


# ============================================================
# 原始 SentenceBoundary → SRT
# ============================================================

def generate_srt_from_boundaries(
    boundaries,
    output_srt
):

    with open(
        output_srt,
        "w",
        encoding="utf-8-sig"
    ) as f:

        index = 1

        for item in boundaries:

            start = (
                item["offset"]
                / 10_000_000
            )

            duration = (
                item["duration"]
                / 10_000_000
            )

            end = start + duration

            text = (
                item["text"]
                .replace("\n", " ")
                .strip()
            )

            if not text:
                continue

            f.write(
                f"{index}\n"
                f"{format_srt_time(start)} --> "
                f"{format_srt_time(end)}\n"
                f"{text}\n\n"
            )

            index += 1


# ============================================================
# 普通句子拆分
# ============================================================

def split_sentences(text):

    sentences = []

    for line in text.splitlines():

        line = line.strip()

        if not line:
            continue

        parts = re.split(
            r'(?<=[.!?。！？])\s+',
            line
        )

        for part in parts:

            part = part.strip()

            if part:
                sentences.append(part)

    if not sentences and text.strip():

        sentences.append(
            text.strip()
        )

    return sentences


# ============================================================
# 判断是否多人文本
#
# 只要出现：
#
# Amala:
# Seraphina:
# Katja:
#
# 这种格式，就进入多人模式。
# ============================================================

def is_multispeaker_text(text):

    pattern = re.compile(
        r'^[A-Za-zÄÖÜäöüß][A-Za-zÄÖÜäöüß0-9 _-]*\s*:',
        re.MULTILINE
    )

    return bool(
        pattern.search(text)
    )


# ============================================================
# 解析多人文本
#
# 输入：
#
# Amala: Guten Morgen!
# Seraphina: Guten Morgen!
#
# 输出：
#
# [
#     {
#         "speaker": "Amala",
#         "text": "Guten Morgen!"
#     },
#     ...
# ]
# ============================================================

def parse_multispeaker_text(text):

    result = []

    current_speaker = None
    current_text = []

    speaker_pattern = re.compile(
        r'^([A-Za-zÄÖÜäöüß][A-Za-zÄÖÜäöüß0-9 _-]*)\s*:\s*(.*)$'
    )

    for raw_line in text.splitlines():

        line = raw_line.strip()

        if not line:
            continue

        match = speaker_pattern.match(
            line
        )

        if match:

            # 保存上一位 Speaker
            if (
                current_speaker
                and current_text
            ):

                combined = " ".join(
                    current_text
                ).strip()

                if combined:

                    result.append({
                        "speaker": current_speaker,
                        "text": combined
                    })

            current_speaker = (
                match.group(1).strip()
            )

            first_text = (
                match.group(2).strip()
            )

            current_text = []

            if first_text:

                current_text.append(
                    first_text
                )

        else:

            # 没有新的 Speaker
            # 如果前面已经有 Speaker，
            # 当前行继续属于上一位
            if current_speaker:

                current_text.append(
                    line
                )

            else:

                # Speaker 之前的普通文本、标题直接忽略
                continue

    # 最后一位 Speaker
    if (
        current_speaker
        and current_text
    ):

        combined = " ".join(
            current_text
        ).strip()

        if combined:

            result.append({
                "speaker": current_speaker,
                "text": combined
            })

    if not result:

        return None

    return result


# ============================================================
# 多人句子拆分
# ============================================================

def split_speaker_sentences(
    speaker,
    text
):

    sentences = []

    parts = re.split(
        r'(?<=[.!?。！？])\s+',
        text
    )

    for part in parts:

        part = part.strip()

        if not part:
            continue

        sentences.append({
            "speaker": speaker,
            "text": part
        })

    return sentences


# ============================================================
# MP3 合并
#
# 使用 FFmpeg
# ============================================================

def merge_mp3_files(
    input_files,
    output_file
):

    if not input_files:

        raise Exception(
            "没有可以合并的 MP3"
        )

    concat_file = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".txt",
        delete=False,
        encoding="utf-8"
    )

    concat_path = concat_file.name

    try:

        for file_path in input_files:

            absolute_path = os.path.abspath(
                file_path
            )

            escaped = (
                absolute_path
                .replace("\\", "/")
                .replace("'", "'\\''")
            )

            concat_file.write(
                f"file '{escaped}'\n"
            )

        concat_file.close()

        command = [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            concat_path,
            "-c",
            "copy",
            output_file
        ]

        print()
        print(
            "[FFMPEG] 开始合并 MP3..."
        )

        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        if result.returncode != 0:

            print(
                result.stderr
            )

            raise Exception(
                "FFmpeg 合并 MP3 失败"
            )

        if (
            not os.path.exists(output_file)
            or
            os.path.getsize(output_file) <= 0
        ):

            raise Exception(
                "合并后的 MP3 无效"
            )

        print(
            f"[FFMPEG] 合并成功: "
            f"{os.path.getsize(output_file)} bytes"
        )

    finally:

        try:
            os.remove(
                concat_path
            )
        except:
            pass


# ============================================================
# 获取 MP3 时长
# ============================================================

def get_mp3_duration(mp3_path):

    try:

        from mutagen.mp3 import MP3

        audio = MP3(
            mp3_path
        )

        duration = float(
            audio.info.length
        )

        if duration > 0:

            return duration

    except Exception as e:

        print(
            f"[MP3] 获取时长失败: {e}"
        )

    return None


# ============================================================
# 多人 TTS
# ============================================================

async def generate_multispeaker_audio(
    text,
    default_voice_id,
    output_mp3,
    timeout=60
):

    parsed = parse_multispeaker_text(
        text
    )

    if not parsed:

        raise Exception(
            "无法解析多人文本"
        )

    sentence_items = []

    # ========================================================
    # Speaker → Sentence
    # ========================================================

    for item in parsed:

        speaker = item["speaker"]
        speaker_text = item["text"]

        sentences = split_speaker_sentences(
            speaker,
            speaker_text
        )

        sentence_items.extend(
            sentences
        )

    print()
    print("=" * 70)
    print(
        f"[MULTI TTS] 共 {len(sentence_items)} 句话"
    )
    print("=" * 70)

    temp_files = []

    boundaries = []

    current_time = 0.0

    try:

        # ====================================================
        # 一句一句 TTS
        # ====================================================

        for index, item in enumerate(
            sentence_items,
            start=1
        ):

            speaker = item["speaker"]
            sentence = item["text"]

            # ------------------------------------------------
            # 直接根据 Speaker 名字寻找 Voice
            # ------------------------------------------------

            voice_id = get_voice_by_tag(
                speaker
            )

            # ------------------------------------------------
            # 如果没找到
            # 使用网页选择的默认 Voice
            # ------------------------------------------------

            if not voice_id:

                voice_id = default_voice_id

                print(
                    f"[MULTI TTS] "
                    f"⚠ 未找到 Speaker: "
                    f"{speaker}"
                )

                print(
                    f"[MULTI TTS] "
                    f"使用默认 Voice: "
                    f"{voice_id}"
                )

            else:

                print(
                    f"[MULTI TTS] "
                    f"{speaker} → "
                    f"{voice_id}"
                )

            sentence_file = os.path.join(
                tempfile.gettempdir(),
                f"multi_tts_{os.getpid()}_{index}.mp3"
            )

            if os.path.exists(
                sentence_file
            ):

                try:
                    os.remove(
                        sentence_file
                    )
                except:
                    pass

            # ------------------------------------------------
            # TTS
            # ------------------------------------------------

            success, _ = (
                await generate_audio_with_boundaries(
                    sentence,
                    voice_id,
                    sentence_file,
                    timeout
                )
            )

            if not success:

                raise Exception(
                    f"第 {index} 句生成失败"
                )

            # ------------------------------------------------
            # 获取真实时长
            # ------------------------------------------------

            duration = get_mp3_duration(
                sentence_file
            )

            if duration is None:

                raise Exception(
                    f"无法获取第 {index} 句音频时长"
                )

            temp_files.append(
                sentence_file
            )

            # ------------------------------------------------
            # Timeline
            # ------------------------------------------------

            start = current_time

            end = (
                current_time
                + duration
            )

            boundaries.append({
                "start": start,
                "end": end,
                "text": sentence,
                "speaker": speaker
            })

            current_time = end

            print(
                f"[MULTI TTS] "
                f"{index}/"
                f"{len(sentence_items)} "
                f"{speaker}: "
                f"{sentence}"
            )

            print(
                f"             "
                f"{start:.3f}s → "
                f"{end:.3f}s"
            )

        # ====================================================
        # 合并 MP3
        # ====================================================

        merge_mp3_files(
            temp_files,
            output_mp3
        )

        # ====================================================
        # 最终时长
        # ====================================================

        final_duration = get_mp3_duration(
            output_mp3
        )

        if final_duration:

            print(
                f"[MULTI TTS] "
                f"最终 MP3: "
                f"{final_duration:.3f}s"
            )

        print()
        print(
            "[MULTI TTS] 🎉 全部完成"
        )

        return True, boundaries

    finally:

        # ====================================================
        # 删除临时文件
        # ====================================================

        for file_path in temp_files:

            try:

                if os.path.exists(
                    file_path
                ):

                    os.remove(
                        file_path
                    )

            except Exception as e:

                print(
                    f"[MULTI TTS] "
                    f"删除临时文件失败: "
                    f"{e}"
                )


# ============================================================
# 多人 SRT
# ============================================================

def generate_multispeaker_srt(
    boundaries,
    output_srt
):

    with open(
        output_srt,
        "w",
        encoding="utf-8-sig"
    ) as f:

        for index, item in enumerate(
            boundaries,
            start=1
        ):

            start = item["start"]
            end = item["end"]

            speaker = item.get(
                "speaker",
                ""
            )

            text = (
                item["text"]
                .replace("\n", " ")
                .strip()
            )

            if not text:
                continue

            # SRT 保留 Speaker
            display_text = (
                f"{speaker}: {text}"
            )

            f.write(
                f"{index}\n"
                f"{format_srt_time(start)} --> "
                f"{format_srt_time(end)}\n"
                f"{display_text}\n\n"
            )


# ============================================================
# 原始 TTS
# ============================================================

async def generate_audio(
    text,
    voice_id,
    output_mp3,
    timeout=60
):

    retry_count = 0

    while True:

        retry_count += 1

        print(
            f"\n第 {retry_count} 次尝试生成..."
        )

        communicate = edge_tts.Communicate(
            text,
            voice_id
        )

        try:

            await asyncio.wait_for(
                communicate.save(
                    output_mp3
                ),
                timeout=timeout
            )

            if (
                os.path.exists(output_mp3)
                and
                os.path.getsize(output_mp3) > 0
            ):

                print(
                    f"[TTS] 成功: "
                    f"{os.path.getsize(output_mp3)} bytes"
                )

                return True

        except Exception as e:

            print(
                f"[错误] {e}"
            )

        if os.path.exists(output_mp3):

            try:
                os.remove(
                    output_mp3
                )

            except:
                pass

        print(
            "生成失败，3秒后自动重试..."
        )

        await asyncio.sleep(3)


# ============================================================
# Request
# ============================================================

class TTSRequest(BaseModel):

    text: str

    voice_key: str

    title: str = "article"


# ============================================================
# TTS API
# ============================================================

@app.post("/tts")
async def tts_endpoint(
    req: TTSRequest
):

    if not req.text.strip():

        raise HTTPException(
            status_code=400,
            detail="文本内容为空"
        )

    if req.voice_key not in VOICE_MAP:

        raise HTTPException(
            status_code=400,
            detail="发音人编号错误"
        )

    voice_id, voice_name, voice_tag = (
        VOICE_MAP[req.voice_key]
    )

    clean_title = safe_filename(
        req.title
    )

    # ========================================================
    # 自动判断单双人模式
    # ========================================================

    multi_mode = is_multispeaker_text(
        req.text
    )

    if multi_mode:

        base_name = (
            f"{clean_title}_MultiSpeaker"
        )

    else:

        base_name = (
            f"{clean_title}_{voice_tag}"
        )

    temp_dir = tempfile.gettempdir()

    output_mp3 = os.path.join(
        temp_dir,
        f"{base_name}.mp3"
    )

    output_srt = os.path.join(
        temp_dir,
        f"{base_name}.srt"
    )

    output_zip = os.path.join(
        ZIP_DIR,
        f"{base_name}.zip"
    )

    print()
    print("=" * 70)

    if multi_mode:

        print(
            "[TTS MODE] 🗣️ 多人 TTS"
        )

    else:

        print(
            "[TTS MODE] 🎙️ 单人 TTS"
        )

    print(
        f"默认声音: {voice_name}"
    )

    print(
        f"字数: {len(req.text)}"
    )

    print("=" * 70)

    # ========================================================
    # 多人模式
    # ========================================================

    if multi_mode:

        try:

            success, boundaries = (
                await generate_multispeaker_audio(
                    req.text,
                    voice_id,
                    output_mp3
                )
            )

        except Exception as e:

            print(
                f"[MULTI TTS ERROR] {e}"
            )

            raise HTTPException(
                status_code=500,
                detail=f"多人音频生成失败: {e}"
            )

        if not success:

            raise HTTPException(
                status_code=500,
                detail="多人音频生成失败"
            )

        generate_multispeaker_srt(
            boundaries,
            output_srt
        )

        print(
            f"[SRT] 多人 SRT 已生成: "
            f"{output_srt}"
        )

    # ========================================================
    # 单人模式
    #
    # 完全保留原来的逻辑
    # ========================================================

    else:

        success, boundaries = (
            await generate_audio_with_boundaries(
                req.text,
                voice_id,
                output_mp3
            )
        )

        if not success:

            raise HTTPException(
                status_code=500,
                detail="音频生成失败"
            )

        generate_srt_from_boundaries(
            boundaries,
            output_srt
        )

        print(
            f"[SRT] 已根据 Edge TTS "
            f"SentenceBoundary 生成: "
            f"{output_srt}"
        )

    # ========================================================
    # 复制到网页根目录
    # ========================================================

    root_mp3 = os.path.join(
        AUDIO_DIR,
        f"{base_name}.mp3"
    )

    root_srt = os.path.join(
        SUBTITLE_DIR,
        f"{base_name}.srt"
    )

    shutil.copy2(
        output_mp3,
        root_mp3
    )

    shutil.copy2(
        output_srt,
        root_srt
    )

    # ========================================================
    # ZIP
    # ========================================================

    with zipfile.ZipFile(
        output_zip,
        "w",
        zipfile.ZIP_DEFLATED
    ) as zf:

        zf.write(
            output_mp3,
            arcname=f"{base_name}.mp3"
        )

        zf.write(
            output_srt,
            arcname=f"{base_name}.srt"
        )

    print(
        f"[ZIP] 已生成: "
        f"{output_zip}"
    )

    print()
    print(
        "🎉 MP3 + SRT + ZIP 全部完成"
    )

    # ========================================================
    # 返回 ZIP
    # ========================================================

    return FileResponse(
        path=output_zip,
        media_type="application/zip",
        filename=f"{base_name}.zip",
        headers={
            "Access-Control-Expose-Headers":
            "Content-Disposition"
        }
    )


# ============================================================
# 课程列表
# ============================================================

@app.get("/api/courses")
async def list_courses():

    files = os.listdir(AUDIO_DIR)

    mp3_files = [
        f
        for f in files
        if f.lower().endswith(".mp3")
    ]

    def natural_sort_key(s):

        return [
            int(text)
            if text.isdigit()
            else text.lower()

            for text in re.split(
                r'([0-9]+)',
                s
            )
        ]

    mp3_files.sort(
        key=natural_sort_key
    )

    courses = []

    for file in mp3_files:

        base_name = file[:-4]

        clean_title = re.sub(
            r'_(Seraphina|Katja|Amala|Conrad|Killian|Florian|MultiSpeaker)$',
            '',
            base_name
        )

        srt_name = (
            f"{base_name}.srt"
        )

        if not os.path.exists(
            os.path.join(
                SUBTITLE_DIR,
                srt_name
            )
        ):

            continue

        courses.append(
            {
                "title": clean_title,
                "audio": f"/audio/{base_name}.mp3",
                "srt": f"/subtitles/{base_name}.srt"
            }
        )

    return courses


# ============================================================
# 静态网页
# ============================================================
app.mount(
    "/audio",
    StaticFiles(
        directory=AUDIO_DIR
    ),
    name="audio"
)

app.mount(
    "/subtitles",
    StaticFiles(
        directory=SUBTITLE_DIR
    ),
    name="subtitles"
)
app.mount(
    "/",
    StaticFiles(
        directory=WEB_DIR,
        html=True
    ),
    name="static"
)


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":

    import webbrowser

    print()
    print("=" * 70)
    print(
        "🚀 Edge TTS 本地服务启动中..."
    )
    print("=" * 70)

    print(
        "📍 http://127.0.0.1:8000"
    )

    print()

    webbrowser.open(
        "http://127.0.0.1:8000/lingq_style_reader.html"
    )

    webbrowser.open(
        "http://127.0.0.1:8000/audio_manager.html"
    )

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8000
    )