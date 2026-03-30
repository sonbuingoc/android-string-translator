#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import concurrent.futures
import json
import re
import time
from copy import deepcopy
from pathlib import Path

import requests
from lxml import etree

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_MODULE_NAME = "app"

MAX_WORKERS = 8
RETRY_COUNT = 3
REQUEST_TIMEOUT = 15

XLIFF_NS = "urn:oasis:names:tc:xliff:document:1.2"
NSMAP = {"xliff": XLIFF_NS}

PRINTF_PLACEHOLDER_PATTERN = re.compile(
    r"%(?:\d+\$)?[-#+ 0,(<]*\d*(?:\.\d+)?(?:[tT])?[a-zA-Z%]"
)
BRACED_PLACEHOLDER_PATTERN = re.compile(r"\{[a-zA-Z0-9_]+\}")
ANDROID_REF_PATTERN = re.compile(r"(?<!\\)(?:@[a-zA-Z0-9_./]+|\?[a-zA-Z0-9_./]+)")
ESCAPE_SEQUENCE_PATTERN = re.compile(r"\\(?:n|t|r|'|\"|@|\?|u[0-9a-fA-F]{4})")
XML_TAG_PATTERN = re.compile(r"</?[^>]+?>", re.DOTALL)
CDATA_PATTERN = re.compile(r"<!\[CDATA\[.*?\]\]>", re.DOTALL)
ENTITY_PATTERN = re.compile(r"&(?:[a-zA-Z]+|#\d+|#x[0-9a-fA-F]+);")
WHITESPACE_ONLY_PATTERN = re.compile(r"^\s*$")


def get_parser():
    return etree.XMLParser(
        remove_blank_text=False,
        strip_cdata=False,
        recover=True,
        remove_comments=False,
    )


def find_source_strings() -> Path:
    print("🔍 Đang tìm strings.xml trong project...")

    candidate = PROJECT_ROOT / APP_MODULE_NAME / "src" / "main" / "res" / "values" / "strings.xml"
    if candidate.exists():
        print(f"✔ Tìm thấy file nguồn: {candidate}")
        return candidate

    print("⚠ Không tìm thấy trong app/src/main/res/values/, thử scan toàn project...")
    matches = list(PROJECT_ROOT.rglob("src/main/res/values/strings.xml"))

    if not matches:
        raise FileNotFoundError("❌ Không tìm thấy strings.xml trong project.")

    chosen = matches[0]
    print(f"✔ Tìm thấy file nguồn: {chosen}")
    return chosen


def locale_to_values_dir(lang_tag: str) -> str:
    parts = lang_tag.split("-")
    if len(parts) == 1:
        return f"values-{parts[0]}"
    return f"values-{parts[0]}-r{parts[1].upper()}"


def android_escape(text: str) -> str:
    """
    Escape các ký tự đặc biệt cho Android XML.
    Lưu ý: Không nên escape < và > nếu chúng là một phần của tag đã được restore.
    Hàm này nên được gọi TRƯỚC khi restore tokens.
    """
    if text is None:
        return ""

    # Bảo vệ các dấu nháy đã được escape trước đó (nếu có)
    protected = text.replace("\\'", "__ESCAPED_SINGLE_QUOTE__")
    protected = protected.replace('\\"', "__ESCAPED_DOUBLE_QUOTE__")

    # Escape các ký tự đặc biệt XML/Android
    protected = protected.replace("&", "&amp;")
    protected = protected.replace("<", "&lt;")
    protected = protected.replace(">", "&gt;")
    protected = protected.replace("'", "\\'")
    protected = protected.replace('"', '\\"')

    # Khôi phục các dấu nháy đã bảo vệ
    protected = protected.replace("__ESCAPED_SINGLE_QUOTE__", "\\'")
    protected = protected.replace("__ESCAPED_DOUBLE_QUOTE__", '\\"')

    return protected


def inner_xml(element) -> str:
    parts = []
    if element.text:
        parts.append(element.text)
    for child in element:
        parts.append(etree.tostring(child, encoding="unicode"))
    return "".join(parts)


def replace_children_preserve_attrs(element, xml_fragment: str):
    attrs = dict(element.attrib)
    nsmap = element.nsmap
    tag = element.tag

    tail = element.tail
    for child in list(element):
        element.remove(child)

    element.attrib.clear()
    element.attrib.update(attrs)
    element.tag = tag
    element.text = None

    # Parse xml_fragment như một phần của XML
    # Nếu fragment chứa các entity như &amp;, etree.fromstring sẽ tự giải mã chúng khi gán vào .text
    wrapper = etree.fromstring(
        f"<wrapper xmlns:xliff=\"{XLIFF_NS}\">{xml_fragment}</wrapper>",
        parser=get_parser(),
    )

    element.text = wrapper.text
    for child in wrapper:
        element.append(child)

    element.tail = tail
    element.attrib.clear()
    element.attrib.update(attrs)


def protect_with_pattern(text: str, pattern: re.Pattern, prefix: str, tokens: list) -> str:
    def repl(match):
        token = f"__{prefix}_{len(tokens)}__"
        tokens.append((token, match.group(0)))
        return token
    return pattern.sub(repl, text)


def protect_all(text: str):
    tokens = []
    protected = text

    protected = protect_with_pattern(protected, CDATA_PATTERN, "CDATA", tokens)
    protected = protect_with_pattern(protected, XML_TAG_PATTERN, "TAG", tokens)
    protected = protect_with_pattern(protected, ENTITY_PATTERN, "ENTITY", tokens)
    protected = protect_with_pattern(protected, PRINTF_PLACEHOLDER_PATTERN, "PRINTF", tokens)
    protected = protect_with_pattern(protected, BRACED_PLACEHOLDER_PATTERN, "BRACE", tokens)
    protected = protect_with_pattern(protected, ANDROID_REF_PATTERN, "REF", tokens)
    protected = protect_with_pattern(protected, ESCAPE_SEQUENCE_PATTERN, "ESC", tokens)

    return protected, tokens


def restore_all(text: str, tokens: list) -> str:
    restored = text
    # Duyệt ngược để tránh việc thay thế các token con (nếu có)
    for token, original in reversed(tokens):
        restored = restored.replace(token, original)
    return restored


def should_translate_text(text: str) -> bool:
    if text is None:
        return False
    if not text.strip():
        return False
    if WHITESPACE_ONLY_PATTERN.match(text):
        return False
    return True


def translate_text(text: str, source_lang: str, target_lang: str, retries: int = RETRY_COUNT) -> str:
    if not should_translate_text(text):
        return text or ""

    protected_text, tokens = protect_all(text)

    url = "https://translate.googleapis.com/translate_a/single"
    params = {
        "client": "gtx",
        "sl": source_lang,
        "tl": target_lang,
        "dt": "t",
        "q": protected_text,
    }

    last_error = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            translated = "".join(chunk[0] for chunk in data[0])

            # QUAN TRỌNG: Escape các ký tự đặc biệt của bản dịch TRƯỚC khi khôi phục tag/placeholder
            translated = android_escape(translated)
            translated = restore_all(translated, tokens)

            return translated
        except Exception as e:
            last_error = e
            if attempt < retries:
                time.sleep(attempt)

    print(f"[ERROR] Dịch thất bại ({source_lang}->{target_lang}): {last_error}")
    # Nếu lỗi, trả về nguyên bản nhưng vẫn phải đảm bảo format XML hợp lệ
    return text


def make_string_item(name: str, text: str):
    return {"kind": "string", "name": name, "text": text or ""}


def make_plural_item(name: str, quantity: str, text: str):
    return {"kind": "plural", "name": name, "quantity": quantity, "text": text or ""}


def make_array_item(name: str, index: int, text: str):
    return {"kind": "string-array", "name": name, "index": index, "text": text or ""}


def item_key(item: dict) -> str:
    if item["kind"] == "string":
        return f"string::{item['name']}"
    if item["kind"] == "plural":
        return f"plural::{item['name']}::{item['quantity']}"
    if item["kind"] == "string-array":
        return f"string-array::{item['name']}::{item['index']}"
    raise ValueError(f"Unknown item kind: {item['kind']}")


def is_comment_node(node) -> bool:
    return isinstance(node, etree._Comment)


def load_source_items(source_file: Path):
    tree = etree.parse(str(source_file), parser=get_parser())
    root = tree.getroot()

    items = []
    resources = []

    for child in root:
        if is_comment_node(child):
            resources.append({"kind": "comment", "element": deepcopy(child)})
            continue

        tag = etree.QName(child).localname

        if tag == "string":
            name = child.get("name")
            if not name:
                resources.append({"kind": "raw", "element": deepcopy(child)})
                continue

            translatable = child.get("translatable")
            skip_translate = translatable is not None and translatable.lower() == "false"

            if not skip_translate:
                items.append(make_string_item(name, inner_xml(child)))

            resources.append({
                "kind": "string",
                "name": name,
                "skip_translate": skip_translate,
                "element": deepcopy(child),
            })
            continue

        if tag == "plurals":
            name = child.get("name")
            if not name:
                resources.append({"kind": "raw", "element": deepcopy(child)})
                continue

            translatable = child.get("translatable")
            skip_translate = translatable is not None and translatable.lower() == "false"

            plural_items = []
            for item_node in child.findall("item"):
                quantity = item_node.get("quantity")
                if not quantity:
                    continue
                obj = make_plural_item(name, quantity, inner_xml(item_node))
                plural_items.append(obj)
                if not skip_translate:
                    items.append(obj)

            resources.append({
                "kind": "plurals",
                "name": name,
                "skip_translate": skip_translate,
                "element": deepcopy(child),
                "items": plural_items,
            })
            continue

        if tag == "string-array":
            name = child.get("name")
            if not name:
                resources.append({"kind": "raw", "element": deepcopy(child)})
                continue

            translatable = child.get("translatable")
            skip_translate = translatable is not None and translatable.lower() == "false"

            array_items = []
            for index, item_node in enumerate(child.findall("item")):
                obj = make_array_item(name, index, inner_xml(item_node))
                array_items.append(obj)
                if not skip_translate:
                    items.append(obj)

            resources.append({
                "kind": "string-array",
                "name": name,
                "skip_translate": skip_translate,
                "element": deepcopy(child),
                "items": array_items,
            })
            continue

        resources.append({"kind": "raw", "element": deepcopy(child)})

    print(f"✔ Đã load {len(items)} mục translatable từ {source_file}")
    return items, resources


def load_existing_translations(module_res_dir: Path, locale_tag: str) -> dict:
    values_dir_name = locale_to_values_dir(locale_tag)
    target_file = module_res_dir / values_dir_name / "strings.xml"

    if not target_file.exists():
        return {}

    tree = etree.parse(str(target_file), parser=get_parser())
    root = tree.getroot()
    existing = {}

    for child in root:
        if is_comment_node(child):
            continue

        tag = etree.QName(child).localname

        if tag == "string":
            name = child.get("name")
            if name:
                existing[f"string::{name}"] = inner_xml(child)

        elif tag == "plurals":
            name = child.get("name")
            if not name:
                continue
            for item_node in child.findall("item"):
                quantity = item_node.get("quantity")
                if quantity:
                    existing[f"plural::{name}::{quantity}"] = inner_xml(item_node)

        elif tag == "string-array":
            name = child.get("name")
            if not name:
                continue
            for index, item_node in enumerate(child.findall("item")):
                existing[f"string-array::{name}::{index}"] = inner_xml(item_node)

    return existing


def is_effectively_translated(existing_value: str, source_value: str) -> bool:
    if not existing_value or not existing_value.strip():
        return False
    if existing_value.strip() == source_value.strip():
        return False
    return True


def translate_item(task):
    index, item, source_lang, target_lang = task
    translated = translate_text(item["text"], source_lang, target_lang)
    return index, item_key(item), translated


def build_output_element(resource: dict, translated_map: dict):
    kind = resource["kind"]

    if kind in {"raw", "comment"}:
        return deepcopy(resource["element"])

    if kind == "string":
        element = deepcopy(resource["element"])
        if not resource["skip_translate"]:
            key = f"string::{resource['name']}"
            if key in translated_map:
                replace_children_preserve_attrs(element, translated_map[key])
        return element

    if kind == "plurals":
        element = deepcopy(resource["element"])
        if not resource["skip_translate"]:
            item_nodes = element.findall("item")
            for item_node in item_nodes:
                quantity = item_node.get("quantity")
                if not quantity:
                    continue
                key = f"plural::{resource['name']}::{quantity}"
                if key in translated_map:
                    replace_children_preserve_attrs(item_node, translated_map[key])
        return element

    if kind == "string-array":
        element = deepcopy(resource["element"])
        if not resource["skip_translate"]:
            item_nodes = element.findall("item")
            for index, item_node in enumerate(item_nodes):
                key = f"string-array::{resource['name']}::{index}"
                if key in translated_map:
                    replace_children_preserve_attrs(item_node, translated_map[key])
        return element

    return deepcopy(resource["element"])


def write_target_strings(module_res_dir: Path, locale_tag: str, resources: list, translated_map: dict):
    values_dir_name = locale_to_values_dir(locale_tag)
    out_dir = module_res_dir / values_dir_name
    out_dir.mkdir(parents=True, exist_ok=True)

    out_file = out_dir / "strings.xml"

    root = etree.Element("resources", nsmap={"xliff": XLIFF_NS})

    for resource in resources:
        root.append(build_output_element(resource, translated_map))

    xml_bytes = etree.tostring(
        root,
        encoding="utf-8",
        xml_declaration=True,
        pretty_print=True,
    )
    out_file.write_bytes(xml_bytes)
    print(f"✔ Xuất file: {out_file}")


def parse_args():
    parser = argparse.ArgumentParser(description="Android string translator")
    parser.add_argument(
        "--skip-translated",
        action="store_true",
        help="Bỏ qua những mục đã có bản dịch trong file strings.xml đích",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=MAX_WORKERS,
        help="Số luồng dịch song song",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    config_path = Path(__file__).parent / "config.json"
    if not config_path.exists():
        raise FileNotFoundError(f"❌ Không tìm thấy file config: {config_path}")

    with config_path.open("r", encoding="utf-8") as f:
        config = json.load(f)

    source_lang = config.get("source_language", "en")
    target_langs = config.get("target_languages", [])

    if not target_langs:
        print("⚠ Không có target_languages trong config.json")
        return

    source_file = find_source_strings()
    module_res_dir = source_file.parent.parent
    source_items, resources = load_source_items(source_file)

    for lang in target_langs:
        print(f"\n🌍 Đang dịch sang: {lang}")

        existing_translations = load_existing_translations(module_res_dir, lang) if args.skip_translated else {}
        translated_map = dict(existing_translations)

        tasks = []
        skipped_count = 0

        for item in source_items:
            key = item_key(item)
            existing_value = existing_translations.get(key, "")
            if args.skip_translated and is_effectively_translated(existing_value, item["text"]):
                skipped_count += 1
                continue
            tasks.append(item)

        total_to_translate = len(tasks)
        completed = 0

        if args.skip_translated:
            print(f"↷ Bỏ qua {skipped_count} mục đã có bản dịch")

        if total_to_translate == 0:
            print(f"✔ Không còn mục nào cần dịch cho {lang}")
            write_target_strings(module_res_dir, lang, resources, translated_map)
            continue

        print(f"📝 Cần dịch {total_to_translate} mục cho {lang}")

        indexed_tasks = [
            (index, item, source_lang, lang)
            for index, item in enumerate(tasks)
        ]

        results = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
            futures = [executor.submit(translate_item, task) for task in indexed_tasks]

            for future in concurrent.futures.as_completed(futures):
                _, key, translated = future.result()
                results[key] = translated
                completed += 1
                print(f"[{completed}/{total_to_translate}] {key}")

        translated_map.update(results)
        write_target_strings(module_res_dir, lang, resources, translated_map)

    print("\n🎉 DONE! Đã dịch xong tất cả ngôn ngữ.")


if __name__ == "__main__":
    main()
