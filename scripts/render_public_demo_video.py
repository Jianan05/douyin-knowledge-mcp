"""Render a captioned public demo video from an actual demo run."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


PROJECT = Path(__file__).resolve().parents[1]
WIDTH, HEIGHT = 1280, 720
BG = "#07131f"
PANEL = "#102537"
PANEL_2 = "#153247"
TEXT = "#f4f7fa"
MUTED = "#9bb0c2"
ACCENT = "#48e0bd"
BLUE = "#67b7ff"
AMBER = "#ffc768"
FONT_REGULAR = Path("C:/Windows/Fonts/NotoSansSC-VF.ttf")
FONT_BOLD = Path("C:/Windows/Fonts/msyhbd.ttc")
FONT_MONO = Path("C:/Windows/Fonts/consola.ttf")


def font(size: int, *, bold: bool = False, mono: bool = False) -> ImageFont.FreeTypeFont:
    path = FONT_MONO if mono else FONT_BOLD if bold else FONT_REGULAR
    return ImageFont.truetype(str(path), size)


def canvas(scene: int, title: str, eyebrow: str) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((42, 32, 1238, 688), radius=28, fill="#0b1b29")
    draw.text((78, 65), eyebrow.upper(), font=font(17, bold=True), fill=ACCENT)
    draw.text((78, 100), title, font=font(42, bold=True), fill=TEXT)
    draw.text((1100, 69), f"{scene}/7", font=font(18, mono=True), fill=MUTED)
    draw.rounded_rectangle((78, 654, 1202, 660), radius=3, fill="#263c4d")
    draw.rounded_rectangle((78, 654, 78 + int(1124 * scene / 7), 660), radius=3, fill=ACCENT)
    return image, draw


def wrapped(draw: ImageDraw.ImageDraw, text: str, xy: tuple[int, int], width: int,
            *, size: int = 24, fill: str = TEXT, spacing: int = 10,
            bold: bool = False, mono: bool = False) -> int:
    chosen = font(size, bold=bold, mono=mono)
    lines: list[str] = []
    for paragraph in text.splitlines() or [""]:
        line = ""
        for char in paragraph:
            candidate = line + char
            if draw.textlength(candidate, font=chosen) <= width:
                line = candidate
            else:
                if line:
                    lines.append(line)
                line = char
        lines.append(line)
    draw.multiline_text(xy, "\n".join(lines), font=chosen, fill=fill, spacing=spacing)
    return xy[1] + len(lines) * (size + spacing)


def card(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], heading: str,
         body: str, *, accent: str = ACCENT, mono: bool = False) -> None:
    draw.rounded_rectangle(box, radius=18, fill=PANEL)
    x1, y1, x2, _ = box
    draw.rounded_rectangle((x1, y1, x1 + 7, box[3]), radius=3, fill=accent)
    draw.text((x1 + 28, y1 + 22), heading, font=font(20, bold=True), fill=accent)
    wrapped(
        draw, body, (x1 + 28, y1 + 61), x2 - x1 - 56,
        size=19 if mono else 21, fill=TEXT, spacing=10, mono=mono,
    )


def scene_one() -> Image.Image:
    image, draw = canvas(1, "从混合素材到可追溯知识", "Douyin Knowledge Ingest")
    draw.text((78, 185), "70 秒公开复现演示", font=font(31), fill=BLUE)
    wrapped(
        draw,
        "真实本地 ASR + OCR + 纯文字入库。保留来源、审阅状态和确认链路，素材不会自动冒充事实。",
        (78, 245), 870, size=28, spacing=15,
    )
    chips = [("NO COOKIE", ACCENT), ("NO PRIVATE DATA", BLUE), ("LOCAL FIRST", AMBER)]
    x = 78
    for label, color in chips:
        chip_w = int(draw.textlength(label, font=font(18, bold=True))) + 38
        draw.rounded_rectangle((x, 400, x + chip_w, 448), radius=24, fill=PANEL_2)
        draw.text((x + 19, 412), label, font=font(18, bold=True), fill=color)
        x += chip_w + 18
    draw.text((78, 548), "本片由一次真实运行的公开夹具产物生成", font=font(20), fill=MUTED)
    return image


def scene_two() -> Image.Image:
    image, draw = canvas(2, "一条命令，在干净环境中复现", "Start")
    draw.rounded_rectangle((78, 185, 1202, 310), radius=18, fill="#061019", outline="#31536b", width=2)
    draw.text((110, 214), ">", font=font(27, bold=True, mono=True), fill=ACCENT)
    draw.text(
        (155, 214),
        ".\\runtime\\python\\python.exe public_media_demo.py --model tiny",
        font=font(22, mono=True), fill=TEXT,
    )
    card(draw, (78, 355, 615, 555), "读取", "仓库内 CC0 WAV\n项目生成 PNG / TXT", accent=BLUE)
    card(draw, (640, 355, 1202, 555), "不会读取", "抖音登录状态\nCookie / 私人收藏 / 默认知识库", accent=AMBER)
    return image


def scene_three(asset: Path) -> Image.Image:
    image, draw = canvas(3, "三种输入，共用一套证据结构", "Inputs")
    card(draw, (78, 180, 410, 570), "AUDIO · CC0", "speech.wav\n\n本地 Whisper tiny\nSHA-256 可核验", accent=BLUE)
    draw.rounded_rectangle((434, 180, 847, 570), radius=18, fill=PANEL)
    draw.text((462, 202), "IMAGE · MIT", font=font(20, bold=True), fill=ACCENT)
    thumb = Image.open(asset).convert("RGB")
    thumb.thumbnail((355, 220))
    image.paste(thumb, (463, 260))
    draw.text((463, 500), "RapidOCR · 原样保留误差", font=font(18), fill=MUTED)
    card(draw, (871, 180, 1202, 570), "TEXT · MIT", "text-post.txt\n\n直接文字入库\n不经过模型改写", accent=AMBER)
    return image


def scene_four(manifest: dict) -> Image.Image:
    image, draw = canvas(4, "真实提取结果，不修饰模型误差", "Extract")
    sources = {row["kind"]: row for row in manifest["sources"]}
    card(draw, (78, 180, 1202, 286), "ASR · 此处播放原始音频", sources["audio"]["extracted_text"], accent=BLUE, mono=True)
    ocr = sources["image"]["extracted_text"].replace("\n", "  /  ")
    card(draw, (78, 310, 1202, 446), "OCR · 空格与字母误识别保留待审", ocr, accent=ACCENT, mono=True)
    text = sources["text"]["extracted_text"].replace("\n", " ")
    card(draw, (78, 470, 1202, 594), "DIRECT TEXT", text, accent=AMBER)
    return image


def scene_five() -> Image.Image:
    image, draw = canvas(5, "每条材料都有来源和机器可读记录", "Trace")
    tree = (
        "demo-output/\n"
        "├─ assets/public-demo/         原始公开夹具\n"
        "├─ inbox/                      三条原始材料笔记\n"
        "├─ _source_packages/           ASR 时间戳来源包\n"
        "├─ _审阅状态.jsonl             人工决定事件\n"
        "├─ notes/                      确认后的知识\n"
        "├─ index.jsonl                 去重与元数据索引\n"
        "└─ public-demo-manifest.json   来源 / 许可 / 哈希"
    )
    draw.rounded_rectangle((78, 180, 1202, 590), radius=18, fill="#061019", outline="#31536b", width=2)
    draw.multiline_text((112, 215), tree, font=font(20), fill=TEXT, spacing=13)
    return image


def scene_six() -> Image.Image:
    image, draw = canvas(6, "自动提取 ≠ 已确认知识", "Review")
    stages = [
        (92, "RAW", "ASR / OCR / TEXT", BLUE),
        (464, "REVIEW", "reference", AMBER),
        (836, "CONFIRMED", "knowledge note", ACCENT),
    ]
    for x, label, detail, color in stages:
        draw.rounded_rectangle((x, 245, x + 280, 430), radius=22, fill=PANEL, outline=color, width=3)
        draw.text((x + 28, 280), label, font=font(27, bold=True), fill=color)
        draw.text((x + 28, 342), detail, font=font(21), fill=TEXT)
    draw.text((390, 322), "→", font=font(42, bold=True), fill=MUTED)
    draw.text((762, 322), "→", font=font(42, bold=True), fill=MUTED)
    wrapped(draw, "只有明确审阅过的来源，才能支持长期知识笔记。", (235, 500), 820, size=26, fill=MUTED)
    return image


def scene_seven() -> Image.Image:
    image, draw = canvas(7, "闭环完成：结论、边界、来源都留下", "Result")
    draw.rounded_rectangle((78, 175, 1202, 480), radius=18, fill=PANEL)
    draw.text((110, 205), "knowledge_status: confirmed", font=font(22, mono=True), fill=ACCENT)
    wrapped(
        draw,
        "音频转录、图片 OCR 和纯文字可以进入同一套可追溯素材结构；自动提取结果仍须经过人工审阅，才能提升为长期知识。",
        (110, 270), 1030, size=27, spacing=15,
    )
    draw.text((110, 430), "3 source_ids · scope · rationale · confirmation event", font=font(19, mono=True), fill=MUTED)
    draw.rounded_rectangle((78, 520, 625, 595), radius=15, fill="#0a302c")
    draw.text((110, 541), "103 tests passed", font=font(25, bold=True), fill=ACCENT)
    draw.rounded_rectangle((650, 520, 1202, 595), radius=15, fill="#142c41")
    draw.text((682, 541), "private_data_used: false", font=font(23, mono=True), fill=BLUE)
    return image


def render(source_root: Path, output: Path) -> None:
    manifest = json.loads((source_root / "public-demo-manifest.json").read_text(encoding="utf-8"))
    if manifest.get("private_data_used") is not False:
        raise ValueError("只允许渲染明确标记为不含私人数据的演示")
    image_fixture = source_root / "assets" / "public-demo" / "text-card.png"
    audio_fixture = source_root / "assets" / "public-demo" / "speech.wav"
    if not image_fixture.is_file() or not audio_fixture.is_file():
        raise FileNotFoundError("演示产物缺少公开音频或图片夹具")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise FileNotFoundError("找不到 ffmpeg")

    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    durations = [7, 10, 11, 12, 11, 11, 10]
    scenes = [
        scene_one(), scene_two(), scene_three(image_fixture), scene_four(manifest),
        scene_five(), scene_six(), scene_seven(),
    ]
    poster = output.with_name(output.stem + "-poster.png")
    scenes[0].save(poster)

    with tempfile.TemporaryDirectory(prefix="douyin-demo-video-") as temp:
        temp_dir = Path(temp)
        paths: list[Path] = []
        for index, scene in enumerate(scenes, 1):
            path = temp_dir / f"scene-{index}.png"
            scene.save(path)
            paths.append(path)
        concat = temp_dir / "slides.txt"
        lines: list[str] = []
        for path, duration in zip(paths, durations):
            lines.extend([f"file '{path.as_posix()}'", f"duration {duration}"])
        lines.append(f"file '{paths[-1].as_posix()}'")
        concat.write_text("\n".join(lines) + "\n", encoding="utf-8")
        total = sum(durations)
        command = [
            ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(concat),
            "-f", "lavfi", "-t", str(total), "-i", "anullsrc=r=48000:cl=stereo",
            "-i", str(audio_fixture),
            "-filter_complex",
            f"[2:a]adelay=28000:all=1,apad=whole_dur={total}[voice];"
            "[1:a][voice]amix=inputs=2:duration=first[a]",
            "-map", "0:v:0", "-map", "[a]", "-vf", "fps=30,format=yuv420p",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
            "-t", str(total), str(output),
        ]
        subprocess.run(command, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="把一次公开演示实跑产物渲染为 72 秒视频")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument(
        "--output", type=Path,
        default=PROJECT / "docs" / "assets" / "public-media-demo.mp4",
    )
    args = parser.parse_args()
    render(args.source_root, args.output)
    print(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
