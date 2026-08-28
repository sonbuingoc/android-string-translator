#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import concurrent.futures
import json
import re
import threading
import time
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_MODULE_NAME = "app"

MAX_WORKERS = 8
RETRY_COUNT = 3
REQUEST_TIMEOUT = 15

ENGINE_GOOGLE = "google"
ENGINE_ARGOS = "argos"
ENGINE_NLLB = "nllb"
SUPPORTED_ENGINES = (ENGINE_GOOGLE, ENGINE_ARGOS, ENGINE_NLLB)
NLLB_MODEL_NAME = "facebook/nllb-200-distilled-600M"

LOCAL_ENGINE_LOCK = threading.Lock()
ARGOS_PACKAGE_INDEX_READY = False
NLLB_STATE = None

NLLB_LANGUAGE_CODES = {
    "af": "afr_Latn",
    "ar": "arb_Arab",
    "bn": "ben_Beng",
    "de": "deu_Latn",
    "en": "eng_Latn",
    "es": "spa_Latn",
    "fil": "tgl_Latn",
    "fr": "fra_Latn",
    "hi": "hin_Deva",
    "id": "ind_Latn",
    "in": "ind_Latn",
    "ko": "kor_Hang",
    "nl": "nld_Latn",
    "pt": "por_Latn",
    "ru": "rus_Cyrl",
    "vi": "vie_Latn",
    "zh": "zho_Hans",
}

XLIFF_NS = "urn:oasis:names:tc:xliff:document:1.2"
NSMAP = {"xliff": XLIFF_NS}

PRINTF_PLACEHOLDER = r"%(?:\d+\$)?[-#+ 0,(<]*\d*(?:\.\d+)?(?:[tT])?[a-zA-Z%]"
PRINTF_PLACEHOLDER_PATTERN = re.compile(rf"(?:{PRINTF_PLACEHOLDER})+")
BRACED_PLACEHOLDER_PATTERN = re.compile(r"\{[a-zA-Z0-9_]+\}")
ANDROID_REF_PATTERN = re.compile(r"(?<!\\)(?:@[a-zA-Z0-9_./]+|\?[a-zA-Z0-9_./]+)")
ESCAPE_SEQUENCE_PATTERN = re.compile(r"\\(?:n|t|r|'|\"|@|\?|u[0-9a-fA-F]{4})")
XML_TAG_PATTERN = re.compile(r"</?[^>]+?>", re.DOTALL)
CDATA_PATTERN = re.compile(r"<!\[CDATA\[.*?\]\]>", re.DOTALL)
ENTITY_PATTERN = re.compile(r"&(?:[a-zA-Z]+|#\d+|#x[0-9a-fA-F]+);")
BARE_AMPERSAND_PATTERN = re.compile(r"&(?![a-zA-Z]+;|#\d+;|#x[0-9a-fA-F]+;)")
WHITESPACE_ONLY_PATTERN = re.compile(r"^\s*$")
PROTECTED_TOKEN_PATTERN = re.compile(r"__[A-Z]+_\d+__")
WORD_PATTERN = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")
PROTECTED_TOKEN_FULL_PATTERN = re.compile(r"__([A-Z]+)_(\d+)__")
PROTECTED_TOKEN_DAMAGED_SUFFIX_PATTERN = re.compile(r"__([A-Z]+)_(\d+)_(?!_)")
PROTECTED_TOKEN_SPACED_PATTERN = re.compile(r"_\s*_?\s*([A-Z]+)\s*_\s*(\d+)\s*_\s*_?")


@dataclass
class RunStats:
    modules_total: int = 0
    modules_processed: int = 0
    languages_processed: int = 0
    items_translated: int = 0
    items_skipped: int = 0
    files_written: int = 0
    warnings: list = field(default_factory=list)
    errors: list = field(default_factory=list)


def log_section(title: str):
    print(f"\n== {title} ==")


def log_info(message: str):
    print(f"INFO  {message}")


def log_success(message: str):
    print(f"OK    {message}")


def log_warning(message: str, stats=None):
    print(f"WARN  {message}")
    if stats is not None:
        stats.warnings.append(message)


def log_error(message: str, stats=None):
    print(f"ERROR {message}")
    if stats is not None:
        stats.errors.append(message)


def run_with_heartbeat(label: str, action, interval_seconds: int = 5):
    done = threading.Event()
    started_at = time.time()

    def heartbeat():
        while not done.wait(interval_seconds):
            elapsed = int(time.time() - started_at)
            log_info(f"{label}... {elapsed}s elapsed")

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        return action()
    finally:
        done.set()
        thread.join(timeout=0.2)


def get_parser():
    return etree.XMLParser(
        remove_blank_text=False,
        strip_cdata=False,
        recover=True,
        remove_comments=False,
    )


def parse_project_root(raw_path: str) -> Path:
    project_root = Path(raw_path).expanduser().resolve()

    if not project_root.is_dir():
        raise argparse.ArgumentTypeError(
            f"project root không tồn tại hoặc không phải thư mục: {project_root}"
        )

    return project_root


def parse_resource_file_name(raw_name: str) -> str:
    resource_file = raw_name.strip()

    if not resource_file:
        raise argparse.ArgumentTypeError("resource file không được rỗng")
    if Path(resource_file).name != resource_file or not resource_file.endswith(".xml"):
        raise argparse.ArgumentTypeError(
            "resource file phải là tên file XML trong thư mục values, ví dụ: strings.xml hoặc arrays.xml"
        )

    return resource_file


def find_source_resource(project_root: Path, resource_file: str) -> Path:
    log_info(f"Finding {resource_file} in project: {project_root}")

    candidate = project_root / APP_MODULE_NAME / "src" / "main" / "res" / "values" / resource_file
    if candidate.exists():
        log_success(f"Source file: {candidate}")
        return candidate

    log_warning("app/src/main/res/values not found; scanning the project for the first matching source file.")
    matches = sorted(project_root.rglob(f"src/main/res/values/{resource_file}"))

    if not matches:
        raise FileNotFoundError(f"Không tìm thấy {resource_file} trong project.")

    chosen = matches[0]
    log_success(f"Source file: {chosen}")
    return chosen


def find_all_source_resources(project_root: Path, resource_file: str):
    log_info(f"Finding all {resource_file} files in project: {project_root}")

    matches = sorted(project_root.rglob(f"src/main/res/values/{resource_file}"))
    if not matches:
        raise FileNotFoundError(f"Không tìm thấy {resource_file} trong project.")

    log_success(f"Found {len(matches)} source file(s):")
    for match in matches:
        print(f"  - {match}")
    return matches


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
        parts.append(escape_xml_text(element.text))
    for child in element:
        parts.append(etree.tostring(child, encoding="unicode"))
    return "".join(parts)


def escape_xml_text(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def normalize_xml_fragment(xml_fragment: str) -> str:
    return BARE_AMPERSAND_PATTERN.sub("&amp;", xml_fragment)


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
        f"<wrapper xmlns:xliff=\"{XLIFF_NS}\">{normalize_xml_fragment(xml_fragment)}</wrapper>",
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

    # Một số ngôn ngữ làm Google Translate sửa nhẹ token bảo vệ,
    # ví dụ tl: __PRINTF_0__ -> __PRINF_0__. Khôi phục theo index nếu prefix gần đúng.
    token_by_index = {}
    for token, original in tokens:
        match = PROTECTED_TOKEN_FULL_PATTERN.fullmatch(token)
        if match:
            token_by_index[int(match.group(2))] = (match.group(1), original)

    def restore_mutated_token(match):
        prefix = match.group(1)
        index = int(match.group(2))
        entry = token_by_index.get(index)
        if not entry:
            return match.group(0)

        expected_prefix, original = entry
        if prefixes_are_close(prefix, expected_prefix):
            return original
        return match.group(0)

    restored = PROTECTED_TOKEN_FULL_PATTERN.sub(restore_mutated_token, restored)
    restored = PROTECTED_TOKEN_DAMAGED_SUFFIX_PATTERN.sub(restore_mutated_token, restored)
    restored = PROTECTED_TOKEN_SPACED_PATTERN.sub(restore_mutated_token, restored)
    return restored


def extract_placeholder_signature(text: str) -> Counter:
    placeholders = []
    placeholders.extend(PRINTF_PLACEHOLDER_PATTERN.findall(text or ""))
    placeholders.extend(BRACED_PLACEHOLDER_PATTERN.findall(text or ""))
    return Counter(placeholders)


def has_broken_protected_token(text: str) -> bool:
    value = text or ""
    return any(
        pattern.search(value)
        for pattern in (
            PROTECTED_TOKEN_PATTERN,
            PROTECTED_TOKEN_DAMAGED_SUFFIX_PATTERN,
            PROTECTED_TOKEN_SPACED_PATTERN,
        )
    )


class TranslationIntegrityError(RuntimeError):
    pass


def format_placeholder_signature(signature: Counter) -> str:
    if not signature:
        return "none"
    parts = []
    for placeholder, count in sorted(signature.items()):
        parts.append(placeholder if count == 1 else f"{placeholder} x{count}")
    return ", ".join(parts)


def validate_translation_integrity(source_text: str, translated_text: str):
    source_signature = extract_placeholder_signature(source_text)
    translated_signature = extract_placeholder_signature(translated_text)

    if source_signature != translated_signature:
        raise TranslationIntegrityError(
            "PLACEHOLDER_INTEGRITY: placeholders changed; "
            f"expected [{format_placeholder_signature(source_signature)}], "
            f"actual [{format_placeholder_signature(translated_signature)}]"
        )

    if has_broken_protected_token(translated_text):
        raise TranslationIntegrityError(
            "PLACEHOLDER_INTEGRITY: protected token could not be restored"
        )


def prefixes_are_close(value: str, expected: str) -> bool:
    if value == expected:
        return True
    if abs(len(value) - len(expected)) > 1:
        return False

    i = 0
    j = 0
    edits = 0
    while i < len(value) and j < len(expected):
        if value[i] == expected[j]:
            i += 1
            j += 1
            continue

        edits += 1
        if edits > 1:
            return False

        if len(value) == len(expected):
            i += 1
            j += 1
        elif len(value) < len(expected):
            j += 1
        else:
            i += 1

    if i < len(value) or j < len(expected):
        edits += 1

    return edits <= 1


def should_translate_text(text: str) -> bool:
    if text is None:
        return False
    if not text.strip():
        return False
    if WHITESPACE_ONLY_PATTERN.match(text):
        return False
    return True


def base_language_code(lang_tag: str) -> str:
    code = (lang_tag or "").strip().replace("_", "-").split("-", 1)[0].lower()
    return "id" if code == "in" else code


def translate_google(protected_text: str, source_lang: str, target_lang: str, retries: int) -> str:
    import requests

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
            return "".join(chunk[0] for chunk in data[0])
        except Exception as error:
            last_error = error
            if attempt < retries:
                time.sleep(attempt)

    raise RuntimeError(f"Google translation failed ({source_lang}->{target_lang}): {last_error}")


def ensure_argos_translation(source_lang: str, target_lang: str):
    global ARGOS_PACKAGE_INDEX_READY

    try:
        import argostranslate.package
        import argostranslate.translate
    except ImportError as error:
        raise RuntimeError(
            "Argos Translate chưa được cài. Chạy: python3 -m pip install argostranslate"
        ) from error

    source_code = base_language_code(source_lang)
    target_code = base_language_code(target_lang)
    if source_code == target_code:
        return None

    installed_languages = argostranslate.translate.get_installed_languages()
    source = next((lang for lang in installed_languages if lang.code == source_code), None)
    target = next((lang for lang in installed_languages if lang.code == target_code), None)
    if source is not None and target is not None:
        try:
            return source.get_translation(target)
        except Exception:
            pass

    with LOCAL_ENGINE_LOCK:
        installed_languages = argostranslate.translate.get_installed_languages()
        source = next((lang for lang in installed_languages if lang.code == source_code), None)
        target = next((lang for lang in installed_languages if lang.code == target_code), None)
        if source is not None and target is not None:
            try:
                return source.get_translation(target)
            except Exception:
                pass

        if not ARGOS_PACKAGE_INDEX_READY:
            log_info("Updating Argos package index...")
            argostranslate.package.update_package_index()
            ARGOS_PACKAGE_INDEX_READY = True

        packages = argostranslate.package.get_available_packages()
        package = next(
            (
                item
                for item in packages
                if item.from_code == source_code and item.to_code == target_code
            ),
            None,
        )
        if package is None:
            raise RuntimeError(f"Argos does not provide language package {source_code}->{target_code}")

        log_info(f"Downloading Argos model {source_code}->{target_code}...")
        package_path = run_with_heartbeat(
            f"Downloading Argos model {source_code}->{target_code}",
            package.download,
        )
        run_with_heartbeat(
            f"Installing Argos model {source_code}->{target_code}",
            lambda: argostranslate.package.install_from_path(package_path),
        )
        log_success(f"Installed Argos model {source_code}->{target_code}")

        installed_languages = argostranslate.translate.get_installed_languages()
        source = next((lang for lang in installed_languages if lang.code == source_code), None)
        target = next((lang for lang in installed_languages if lang.code == target_code), None)
        if source is None or target is None:
            raise RuntimeError(f"Argos model installed but language pair is unavailable: {source_code}->{target_code}")
        return source.get_translation(target)


def translate_argos(protected_text: str, source_lang: str, target_lang: str) -> str:
    if base_language_code(source_lang) == base_language_code(target_lang):
        return protected_text
    translation = ensure_argos_translation(source_lang, target_lang)
    return translation.translate(protected_text)


def nllb_language_code(lang_tag: str) -> str:
    base = base_language_code(lang_tag)
    code = NLLB_LANGUAGE_CODES.get(base)
    if not code:
        raise RuntimeError(f"NLLB language mapping is not configured for: {lang_tag}")
    return code


def get_nllb_state():
    global NLLB_STATE
    if NLLB_STATE is not None:
        return NLLB_STATE

    with LOCAL_ENGINE_LOCK:
        if NLLB_STATE is not None:
            return NLLB_STATE
        try:
            import torch
            from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
        except ImportError as error:
            raise RuntimeError(
                "NLLB dependencies chưa được cài. Chạy: python3 -m pip install torch transformers sentencepiece"
            ) from error

        log_info(f"Loading NLLB model {NLLB_MODEL_NAME}. The first run may download the model...")
        tokenizer = run_with_heartbeat(
            "Loading NLLB tokenizer",
            lambda: AutoTokenizer.from_pretrained(NLLB_MODEL_NAME),
        )
        model = run_with_heartbeat(
            "Downloading/loading NLLB model",
            lambda: AutoModelForSeq2SeqLM.from_pretrained(NLLB_MODEL_NAME),
        )
        model.eval()
        NLLB_STATE = (torch, tokenizer, model)
        log_success("NLLB model is ready.")
        return NLLB_STATE


def translate_nllb(protected_text: str, source_lang: str, target_lang: str) -> str:
    source_code = nllb_language_code(source_lang)
    target_code = nllb_language_code(target_lang)
    if source_code == target_code:
        return protected_text

    torch, tokenizer, model = get_nllb_state()
    tokenizer.src_lang = source_code
    inputs = tokenizer(protected_text, return_tensors="pt", truncation=True, max_length=512)
    target_id = tokenizer.convert_tokens_to_ids(target_code)
    if target_id is None or target_id == tokenizer.unk_token_id:
        raise RuntimeError(f"NLLB target language is unavailable: {target_code}")
    with torch.no_grad():
        generated = model.generate(
            **inputs,
            forced_bos_token_id=target_id,
            max_new_tokens=512,
        )
    return tokenizer.batch_decode(generated, skip_special_tokens=True)[0]


def translate_engine_text(
    text: str,
    source_lang: str,
    target_lang: str,
    retries: int,
    engine: str,
) -> str:
    if engine == ENGINE_GOOGLE:
        return translate_google(text, source_lang, target_lang, retries)
    if engine == ENGINE_ARGOS:
        return translate_argos(text, source_lang, target_lang)
    if engine == ENGINE_NLLB:
        return translate_nllb(text, source_lang, target_lang)
    raise RuntimeError(f"Unsupported translation engine: {engine}")


def translate_plain_segment(
    segment: str,
    source_lang: str,
    target_lang: str,
    retries: int,
    engine: str,
) -> str:
    if not segment or not any(char.isalpha() for char in segment):
        return segment

    match = re.fullmatch(r"(\s*)(.*?)(\s*)", segment, re.DOTALL)
    if not match:
        return segment

    leading, core, trailing = match.groups()
    if not core or not any(char.isalpha() for char in core):
        return segment

    translated = translate_engine_text(core, source_lang, target_lang, retries, engine)
    return leading + android_escape(translated) + trailing


def translate_segmented_text(
    text: str,
    source_lang: str,
    target_lang: str,
    retries: int,
    engine: str,
) -> str:
    protected_text, tokens = protect_all(text)
    token_map = dict(tokens)
    parts = []
    last_end = 0

    for match in PROTECTED_TOKEN_PATTERN.finditer(protected_text):
        plain_segment = protected_text[last_end:match.start()]
        parts.append(translate_plain_segment(plain_segment, source_lang, target_lang, retries, engine))
        parts.append(token_map.get(match.group(0), match.group(0)))
        last_end = match.end()

    trailing_segment = protected_text[last_end:]
    parts.append(translate_plain_segment(trailing_segment, source_lang, target_lang, retries, engine))
    return "".join(parts)


def translate_text(
    text: str,
    source_lang: str,
    target_lang: str,
    retries: int = RETRY_COUNT,
    engine: str = ENGINE_GOOGLE,
) -> str:
    if not should_translate_text(text):
        return text or ""

    translated = translate_segmented_text(text, source_lang, target_lang, retries, engine)
    validate_translation_integrity(text, translated)
    return translated


def translate_words_in_segment(segment: str, source_lang: str, target_lang: str, engine: str) -> str:
    parts = []
    last_end = 0

    for match in WORD_PATTERN.finditer(segment):
        parts.append(android_escape(segment[last_end:match.start()]))
        parts.append(translate_text(match.group(0), source_lang, target_lang, engine=engine))
        last_end = match.end()

    parts.append(android_escape(segment[last_end:]))
    return "".join(parts)


def translate_word_by_word(text: str, source_lang: str, target_lang: str, engine: str) -> str:
    if not should_translate_text(text):
        return text or ""

    protected_text, tokens = protect_all(text)
    parts = []
    last_end = 0

    for match in PROTECTED_TOKEN_PATTERN.finditer(protected_text):
        parts.append(translate_words_in_segment(protected_text[last_end:match.start()], source_lang, target_lang, engine))
        parts.append(match.group(0))
        last_end = match.end()

    parts.append(translate_words_in_segment(protected_text[last_end:], source_lang, target_lang, engine))
    restored = restore_all("".join(parts), tokens)
    validate_translation_integrity(text, restored)
    return restored


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

    log_success(f"Loaded {len(items)} translatable item(s) from {source_file}")
    return items, resources


def load_existing_translations(module_res_dir: Path, locale_tag: str, resource_file: str) -> dict:
    values_dir_name = locale_to_values_dir(locale_tag)
    target_file = module_res_dir / values_dir_name / resource_file

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


def has_existing_translation(existing_value: str) -> bool:
    if not existing_value or not existing_value.strip():
        return False
    return True


def translate_item(task):
    index, item, source_lang, target_lang, word_by_word, engine = task
    key = item_key(item)
    try:
        if word_by_word:
            translated = translate_word_by_word(item["text"], source_lang, target_lang, engine)
        else:
            translated = translate_text(item["text"], source_lang, target_lang, engine=engine)
        return index, key, translated, None
    except TranslationIntegrityError as error:
        return index, key, item["text"], str(error)
    except Exception as error:
        return index, key, item["text"], f"TRANSLATION_FAILED: {error}"


def parse_id_filters(raw_ids):
    if not raw_ids:
        return set()

    ids = set()
    for raw in raw_ids:
        for value in raw.split(","):
            normalized = value.strip()
            if normalized:
                ids.add(normalized)
    return ids


def item_matches_id_filter(item: dict, id_filters) -> bool:
    if not id_filters:
        return True

    return item["name"] in id_filters or item_key(item) in id_filters


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


def write_target_resource(module_res_dir: Path, locale_tag: str, resource_file: str, resources: list, translated_map: dict):
    values_dir_name = locale_to_values_dir(locale_tag)
    out_dir = module_res_dir / values_dir_name
    out_dir.mkdir(parents=True, exist_ok=True)

    out_file = out_dir / resource_file

    root = etree.Element("resources", nsmap={"xliff": XLIFF_NS})

    for resource in resources:
        if resource.get("skip_translate"):
            continue
        root.append(build_output_element(resource, translated_map))

    xml_bytes = etree.tostring(
        root,
        encoding="utf-8",
        xml_declaration=True,
        pretty_print=True,
    )
    out_file.write_bytes(xml_bytes)
    log_success(f"Wrote {out_file}")
    return out_file


def parse_args():
    parser = argparse.ArgumentParser(description="Android string translator")
    parser.add_argument(
        "--skip-translated",
        action="store_true",
        help="Bỏ qua những mục đã có bản dịch trong file resource đích",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=MAX_WORKERS,
        help="Số luồng dịch song song",
    )
    parser.add_argument(
        "--word-by-word",
        action="store_true",
        help="Dịch từng từ riêng lẻ thay vì dịch cả câu/cụm",
    )
    parser.add_argument(
        "--ids",
        nargs="+",
        help=(
            "Chỉ dịch các resource id được chỉ định. "
            "Hỗ trợ dạng cách nhau bằng dấu cách hoặc dấu phẩy, ví dụ: --ids app_name title"
        ),
    )
    parser.add_argument(
        "--project-root",
        type=parse_project_root,
        default=DEFAULT_PROJECT_ROOT,
        help=(
            "Đường dẫn Android project cần dịch. "
            f"Mặc định: {DEFAULT_PROJECT_ROOT}"
        ),
    )
    parser.add_argument(
        "--resource-file",
        type=parse_resource_file_name,
        default=None,
        help="Tên một file resource trong values cần dịch (tương thích CLI cũ)",
    )
    parser.add_argument(
        "--resource-files",
        nargs="+",
        type=parse_resource_file_name,
        help="Một hoặc nhiều tên file resource trong values cần dịch",
    )
    parser.add_argument(
        "--resource-paths",
        nargs="+",
        help="Một hoặc nhiều đường dẫn resource XML tương đối từ project root",
    )
    parser.add_argument(
        "--engine",
        choices=SUPPORTED_ENGINES,
        default=ENGINE_GOOGLE,
        help="Translation engine: google, argos hoặc nllb",
    )
    parser.add_argument(
        "--all-modules",
        action="store_true",
        help="Dịch resource file này trong tất cả module có src/main/res/values",
    )
    return parser.parse_args()


def translate_source_file(source_file: Path, args, source_lang: str, target_langs, id_filters, stats: RunStats):
    module_res_dir = source_file.parent.parent
    resource_file = source_file.name
    source_items, resources = load_source_items(source_file)

    if id_filters:
        matched_ids = {item["name"] for item in source_items if item_matches_id_filter(item, id_filters)}
        unmatched_ids = id_filters - matched_ids - {item_key(item) for item in source_items}
        log_info(f"ID filter matched {len(matched_ids)} resource id(s).")
        if unmatched_ids:
            log_warning(f"ID not found in source file: {', '.join(sorted(unmatched_ids))}", stats)

    if args.word_by_word:
        log_info("Word-by-word mode is enabled.")

    for lang_index, lang in enumerate(target_langs, start=1):
        log_section(f"Language [{lang_index}/{len(target_langs)}] {lang}")

        existing_translations = (
            load_existing_translations(module_res_dir, lang, resource_file)
            if args.skip_translated or id_filters
            else {}
        )
        translated_map = dict(existing_translations)

        tasks = []
        skipped_count = 0

        for item in source_items:
            if not item_matches_id_filter(item, id_filters):
                continue

            key = item_key(item)
            existing_value = existing_translations.get(key, "")
            if args.skip_translated and has_existing_translation(existing_value):
                skipped_count += 1
                continue
            tasks.append(item)

        total_to_translate = len(tasks)
        completed = 0
        stats.languages_processed += 1
        stats.items_skipped += skipped_count

        effective_workers = max(1, args.workers)
        log_info(
            "Items: total={total}, translate={translate}, skipped={skipped}, workers={workers}, engine={engine}".format(
                total=len(source_items),
                translate=total_to_translate,
                skipped=skipped_count,
                workers=effective_workers,
                engine=args.engine,
            )
        )

        if total_to_translate == 0:
            log_success(f"No pending translations for {lang}.")
            write_target_resource(module_res_dir, lang, resource_file, resources, translated_map)
            stats.files_written += 1
            continue

        indexed_tasks = [
            (index, item, source_lang, lang, args.word_by_word, args.engine)
            for index, item in enumerate(tasks)
        ]

        results = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=effective_workers) as executor:
            futures = [executor.submit(translate_item, task) for task in indexed_tasks]

            for future in concurrent.futures.as_completed(futures):
                _, key, translated, error = future.result()
                results[key] = translated
                completed += 1
                if error:
                    log_error(f"[{lang}] {key}: {error}", stats)
                else:
                    stats.items_translated += 1
                print(f"  [{completed}/{total_to_translate}] {key}")

        translated_map.update(results)
        write_target_resource(module_res_dir, lang, resource_file, resources, translated_map)
        stats.files_written += 1


def main():
    args = parse_args()
    id_filters = parse_id_filters(args.ids)
    stats = RunStats()

    config_path = Path(__file__).parent / "config.json"
    if not config_path.exists():
        raise FileNotFoundError(f"Không tìm thấy file config: {config_path}")

    with config_path.open("r", encoding="utf-8") as f:
        config = json.load(f)

    source_lang = config.get("source_language", "en")
    target_langs = config.get("target_languages", [])

    log_info(f"Translation engine: {args.engine}")

    if not target_langs:
        log_warning("Không có target_languages trong config.json", stats)
        return

    source_files = []
    seen_paths = set()

    if args.resource_paths:
        for raw_path in args.resource_paths:
            candidate = (args.project_root / raw_path).resolve()
            try:
                candidate.relative_to(args.project_root)
            except ValueError as error:
                raise ValueError(f"Resource path nằm ngoài project root: {raw_path}") from error
            if not candidate.is_file() or candidate.suffix.lower() != ".xml":
                raise FileNotFoundError(f"Không tìm thấy resource file: {candidate}")
            if candidate not in seen_paths:
                source_files.append(candidate)
                seen_paths.add(candidate)
    else:
        resource_files = args.resource_files or ([args.resource_file] if args.resource_file else ["strings.xml"])
        for resource_file in resource_files:
            matches = (
                find_all_source_resources(args.project_root, resource_file)
                if args.all_modules
                else [find_source_resource(args.project_root, resource_file)]
            )
            for match in matches:
                if match not in seen_paths:
                    source_files.append(match)
                    seen_paths.add(match)

    stats.modules_total = len(source_files)

    for index, source_file in enumerate(source_files, start=1):
        module_dir = source_file.parents[4]
        log_section(
            f"Resource [{index}/{len(source_files)}] {source_file.name} | module {module_dir.name}"
        )
        translate_source_file(source_file, args, source_lang, target_langs, id_filters, stats)
        stats.modules_processed += 1

    log_section("Summary")
    log_success(f"Modules processed: {stats.modules_processed}/{stats.modules_total}")
    log_success(f"Languages processed: {stats.languages_processed}")
    log_success(f"Items translated: {stats.items_translated}")
    log_success(f"Items skipped: {stats.items_skipped}")
    log_success(f"Files written: {stats.files_written}")

    if stats.warnings:
        log_warning(f"Warnings: {len(stats.warnings)}")
        for warning in stats.warnings[:10]:
            print(f"  - {warning}")
        if len(stats.warnings) > 10:
            print(f"  - ... {len(stats.warnings) - 10} more")

    if stats.errors:
        log_error(f"Errors: {len(stats.errors)}")
        for error in stats.errors[:10]:
            print(f"  - {error}")
        if len(stats.errors) > 10:
            print(f"  - ... {len(stats.errors) - 10} more")
    else:
        log_success("Completed without translation errors.")


if __name__ == "__main__":
    main()
