#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
from pathlib import Path
import xml.etree.ElementTree as ET
import concurrent.futures
import requests

def find_project_root(start: Path) -> Path:
    cur = start
    while cur != cur.parent:
        if (cur / "settings.gradle").exists() or (cur / "settings.gradle.kts").exists():
            return cur
        cur = cur.parent
    raise RuntimeError("❌ Không tìm thấy settings.gradle → không phải Android project")


PROJECT_ROOT = find_project_root(Path(__file__).resolve())

# =========================
# Helper: tìm file strings.xml nguồn
# =========================

def find_source_strings() -> Path:
    print("🔍 Đang tìm strings.xml trong Android project...")

    matches = list(
        PROJECT_ROOT.rglob("src/main/res/values/strings.xml")
    )

    if not matches:
        raise FileNotFoundError(
            "❌ Không tìm thấy strings.xml trong bất kỳ module nào (src/main/res/values)"
        )

    # Ưu tiên module tên là app
    for p in matches:
        if "/app/" in str(p).replace("\\", "/"):
            print(f"✔ Tìm thấy file nguồn (app): {p}")
            return p

    # Fallback: lấy file đầu tiên
    chosen = matches[0]
    print(f"✔ Tìm thấy file nguồn: {chosen}")
    return chosen



# =========================
# Helper: mapping locale -> thư mục values-*
# =========================

def locale_to_values_dir(lang_tag: str) -> str:
    """
    'fr'      -> 'values-fr'
    'pt-BR'   -> 'values-pt-rBR'
    'en-GB'   -> 'values-en-rGB'
    'af-ZA'   -> 'values-af-rZA'
    """
    parts = lang_tag.split("-")
    if len(parts) == 1:
        # Chỉ có language
        return f"values-{parts[0]}"
    else:
        lang = parts[0]
        region = parts[1].upper()
        return f"values-{lang}-r{region}"


# =========================
# Android escape
# =========================

def android_escape(text: str) -> str:
    """
    Escape string cho Android:
    - Bảo vệ \' đã có sẵn, không double-escape.
    - Escape &, <, >
    - Escape ' còn lại thành \'
    """
    if text is None:
        return ""

    # 0) Bảo vệ các \' đã có sẵn
    PROTECTED_TOKEN = "__ESCAPED_SINGLE_QUOTE__"
    protected = text.replace("\\'", PROTECTED_TOKEN)

    # 1) Escape các ký tự XML cơ bản
    protected = protected.replace("&", "&amp;")
    protected = protected.replace("<", "&lt;")
    protected = protected.replace(">", "&gt;")

    # 2) Escape dấu nháy đơn còn lại
    protected = protected.replace("'", "\\'")

    # 3) Khôi phục lại các \' ban đầu
    result = protected.replace(PROTECTED_TOKEN, "\\'")

    return result


# =========================
# Translate qua Google (free endpoint)
# =========================

def translate_text(text: str, source_lang: str, target_lang: str) -> str:
    if not text.strip():
        return text

    url = "https://translate.googleapis.com/translate_a/single"
    params = {
        "client": "gtx",
        "sl": source_lang,
        "tl": target_lang,
        "dt": "t",
        "q": text
    }

    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        # data[0] là list các đoạn, mỗi đoạn [translated, original, ...]
        translated = "".join(chunk[0] for chunk in data[0])
        return translated
    except Exception as e:
        print(f"[ERROR] Dịch thất bại ({source_lang}->{target_lang}): {e}")
        # fallback: trả lại text gốc
        return text


# =========================
# Đọc strings nguồn, bỏ qua translatable="false"
# =========================

def load_source_strings(source_file: Path) -> dict:
    tree = ET.parse(source_file)
    root = tree.getroot()

    strings = {}

    for item in root.findall("string"):
        name = item.get("name")
        if not name:
            continue

        # Bỏ qua translatable="false"
        translatable = item.get("translatable")
        if translatable is not None and translatable.lower() == "false":
            # print(f"↷ Bỏ qua (translatable=false): {name}")
            continue

        value = item.text or ""
        strings[name] = value

    print(f"✔ Đã load {len(strings)} string translatable từ {source_file}")
    return strings


# =========================
# Dịch 1 item (dùng cho ThreadPool)
# =========================

def translate_item(args):
    key, value, source_lang, target_lang = args
    translated = translate_text(value, source_lang, target_lang)
    escaped = android_escape(translated)
    return key, escaped


# =========================
# Ghi file strings.xml đích
# =========================

def write_target_strings(module_res_dir: Path, locale_tag: str, translated_map: dict):
    """
    module_res_dir: ví dụ /<project>/app/src/main/res
    locale_tag: ví dụ 'pt-BR'
    """
    values_dir_name = locale_to_values_dir(locale_tag)
    out_dir = module_res_dir / values_dir_name
    out_dir.mkdir(parents=True, exist_ok=True)

    out_file = out_dir / "strings.xml"

    lines = ['<resources>']
    for key, value in translated_map.items():
        lines.append(f'    <string name="{key}">{value}</string>')
    lines.append('</resources>')

    out_file.write_text("\n".join(lines), encoding="utf-8")
    print(f"✔ Xuất file: {out_file}")


# =========================
# Main
# =========================

def main():
    # Đọc config
    config_path = Path(__file__).parent / "config.json"
    if not config_path.exists():
        raise FileNotFoundError(f"❌ Không tìm thấy file config: {config_path}")

    with config_path.open("r", encoding="utf-8") as f:
        config = json.load(f)

    source_lang = config.get("source_language", "en")
    target_langs = config.get("target_languages", [])

    # Tìm file nguồn
    source_file = find_source_strings()

    # app/src/main/res
    module_res_dir = source_file.parent.parent  # .../res

    # Load strings nguồn (bỏ qua translatable="false")
    strings_map = load_source_strings(source_file)

    # Dịch lần lượt từng ngôn ngữ
    for lang in target_langs:
        print(f"\n🌍 Đang dịch sang: {lang}")

        tasks = [
            (key, value, source_lang, lang)
            for key, value in strings_map.items()
        ]

        translated_map = {}

        # Dịch song song
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            for key, escaped_value in executor.map(translate_item, tasks):
                translated_map[key] = escaped_value

        # Ghi file ra đúng thư mục values-*
        write_target_strings(module_res_dir, lang, translated_map)

    print("\n🎉 DONE! Đã dịch xong tất cả ngôn ngữ.")


if __name__ == "__main__":
    main()
