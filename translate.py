import os
import json
import argparse
import requests
import xml.etree.ElementTree as ET
from xml.dom import minidom
from concurrent.futures import ThreadPoolExecutor, as_completed

from escape import android_escape


# ==================== CONFIG ====================

def load_config():
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    return cfg["source_language"], cfg["target_languages"], cfg["cache_file"]


# ==================== SAFE PROJECT DETECTOR ====================

def find_strings_in_current_project(script_dir):
    """
    Chỉ tìm strings.xml TRONG PROJECT chứa submodule.
    Tuyệt đối không scan toàn workspace.
    """

    project_root = os.path.abspath(os.path.join(script_dir, ".."))

    # 1. Ưu tiên module "app"
    app_strings = os.path.join(
        project_root, "app", "src", "main", "res", "values", "strings.xml"
    )
    if os.path.exists(app_strings):
        return app_strings

    # 2. Nếu không có app → tìm module đầu tiên
    candidates = []
    for module in os.listdir(project_root):
        module_path = os.path.join(project_root, module)
        if not os.path.isdir(module_path):
            continue

        f = os.path.join(module_path, "src", "main", "res", "values", "strings.xml")
        if os.path.exists(f):
            candidates.append(f)

    if not candidates:
        print("❌ Không tìm thấy strings.xml trong project hiện tại.")
        raise SystemExit(1)

    return candidates[0]


def resolve_res_root(strings_file):
    """
    From: <project>/app/src/main/res/values/strings.xml
    → To:  <project>/app/src/main/res
    """
    return strings_file.split("/values/")[0]


# ==================== TRANSLATION ====================

MAX_WORKERS = 20
BATCH_SIZE = 10

LOCALE_MAP = {
    "in": "id",
    "af-ZA": "af",
    "en-PH": "en",
    "en-CA": "en",
    "en-GB": "en"
}


def api_locale(locale: str) -> str:
    return LOCALE_MAP.get(locale, locale)


def translate_text(text, src, to):
    try:
        resp = requests.get(
            "https://api.mymemory.translated.net/get",
            params={"q": text, "langpair": f"{src}|{to}"},
            timeout=10
        )
        resp.raise_for_status()
        return resp.json().get("responseData", {}).get("translatedText", text)
    except:
        return text


def batch_list(lst, size):
    for i in range(0, len(lst), size):
        yield lst[i:i + size]


def translate_locale_batch(key_map, src, locale):
    target = api_locale(locale)

    # Nếu cùng ngôn ngữ → giữ nguyên
    if target.split("-")[0] == src:
        return {k: v for k, v in key_map.items()}

    results = {}

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as exe:
        future_map = {}

        for chunk in batch_list(list(key_map.items()), BATCH_SIZE):
            keys = [k for k, _ in chunk]
            texts = [v for _, v in chunk]
            joined = "\n###\n".join(texts)

            future = exe.submit(translate_text, joined, src, target)
            future_map[future] = (keys, texts)

        for future in as_completed(future_map):
            keys, texts = future_map[future]
            out = future.result()
            lines = out.split("\n###\n")

            for i, key in enumerate(keys):
                results[key] = lines[i]

    return results


# ==================== XML PARSE ====================

def collect_texts(root):
    key_map = {}

    for child in root:
        tag = child.tag

        if tag == "string":
            if child.get("translatable", "true") == "false":
                continue
            key = f"string::{child.get('name')}"
            text = (child.text or "").strip()
            if text:
                key_map[key] = text

        elif tag == "plurals":
            if child.get("translatable", "true") == "false":
                continue
            name = child.get("name")
            for item in child.findall("item"):
                qty = item.get("quantity")
                text = (item.text or "").strip()
                if text:
                    key_map[f"plurals::{name}::{qty}"] = text

        elif tag == "string-array":
            if child.get("translatable", "true") == "false":
                continue
            name = child.get("name")
            for i, item in enumerate(child.findall("item")):
                text = (item.text or "").strip()
                if text:
                    key_map[f"array::{name}::{i}"] = text

    return key_map


def build_translated_xml(root, translated):
    new_root = ET.Element("resources")

    for child in root:
        tag = child.tag

        if tag == "string":
            name = child.get("name")
            attrs = {"name": name}
            if child.get("translatable") == "false":
                attrs["translatable"] = "false"

            elem = ET.SubElement(new_root, "string", attrs)
            original = child.text or ""

            if child.get("translatable") == "false":
                elem.text = android_escape(original)
            else:
                key = f"string::{name}"
                elem.text = android_escape(translated.get(key, original))

        elif tag == "plurals":
            name = child.get("name")
            attrs = {"name": name}
            if child.get("translatable") == "false":
                attrs["translatable"] = "false"

            p = ET.SubElement(new_root, "plurals", attrs)

            for item in child.findall("item"):
                qty = item.get("quantity")
                original = item.text or ""
                it = ET.SubElement(p, "item", {"quantity": qty})

                if child.get("translatable") == "false":
                    it.text = android_escape(original)
                else:
                    key = f"plurals::{name}::{qty}"
                    it.text = android_escape(translated.get(key, original))

        elif tag == "string-array":
            name = child.get("name")
            attrs = {"name": name}
            if child.get("translatable") == "false":
                attrs["translatable"] = "false"

            arr = ET.SubElement(new_root, "string-array", attrs)

            for i, item in enumerate(child.findall("item")):
                original = item.text or ""
                it = ET.SubElement(arr, "item")
                if child.get("translatable") == "false":
                    it.text = android_escape(original)
                else:
                    key = f"array::{name}::{i}"
                    it.text = android_escape(translated.get(key, original))

        else:
            new_root.append(child)

    return new_root


# ==================== MAIN ====================

def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))

    # Chỉ tìm strings.xml TRONG PROJECT HIỆN TẠI
    strings_file = find_strings_in_current_project(script_dir)
    res_root = resolve_res_root(strings_file)

    source_lang, target_locales, cache_file = load_config()

    print(f"\n✔ Project root: {os.path.abspath(os.path.join(script_dir, '..'))}")
    print(f"✔ Using strings.xml: {strings_file}")
    print(f"✔ Output to: {res_root}")

    tree = ET.parse(strings_file)
    root = tree.getroot()

    key_map = collect_texts(root)
    print(f"🔍 Keys detected: {len(key_map)}")

    for locale in target_locales:
        print(f"\n🌍 Translating → {locale}")

        translated = translate_locale_batch(key_map, source_lang, locale)
        new_root = build_translated_xml(root, translated)

        folder = f"values-{locale}"
        if "-" in locale:
            lang, region = locale.split("-")
            folder = f"values-{lang}-r{region.upper()}"

        out_dir = os.path.join(res_root, folder)
        out_file = os.path.join(out_dir, "strings.xml")

        os.makedirs(out_dir, exist_ok=True)

        xml_str = minidom.parseString(
            ET.tostring(new_root, encoding="utf-8")
        ).toprettyxml(indent="    ")

        with open(out_file, "w", encoding="utf-8") as f:
            f.write(xml_str)

        print(f"✔ Done: {out_file}")

    print("\n🎉 FINISHED — SAFE MODE!")


if __name__ == "__main__":
    main()
