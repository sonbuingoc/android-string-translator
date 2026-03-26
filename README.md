# android-string-translator

Công cụ Python giúp dịch tự động `strings.xml` cho Android project.

Tool này phù hợp khi bạn muốn tạo nhanh các file đa ngôn ngữ từ `values/strings.xml` mà không cần thao tác thủ công trong Android Studio.

## Tính năng

- Tự động tìm file `strings.xml` nguồn
- Bỏ qua các resource có `translatable="false"`
- Hỗ trợ:
  - `string`
  - `plurals`
  - `string-array`
- Tạo đúng thư mục Android resource:
  - `values-fr`
  - `values-vi`
  - `values-pt-rBR`
- Dịch song song để tăng tốc
- Hiển thị tiến trình theo từng ngôn ngữ, ví dụ `1/37`
- Có option bỏ qua các mục đã dịch với `--skip-translated`
- Bảo vệ placeholder và format Android tốt hơn:
  - `%s`, `%1$s`, `%d`, `%1$.2f`, `%%`
  - `{name}`, `{count}`
  - `\n`, `\t`, `\'`, `\"`
  - `@string/...`, `?attr/...`
  - tag như `<b>`, `<i>`, `<u>`, `<xliff:g>`
- Không phụ thuộc Android Studio

## Requirements

- macOS / Linux / Windows
- Python 3.8+
- Internet connection
- Google Translate free endpoint

## Cài đặt

### 1. Clone repository

```bash
git clone https://github.com/sonbuingoc/android-string-translator.git
cd android-string-translator
```

### 2. Cài dependencies

Nếu bạn dùng bản script với `requests`:

```bash
python3 -m pip install --upgrade pip
python3 -m pip install requests
```

Nếu bạn dùng bản nâng cao với `lxml` để giữ XML/comment/CDATA tốt hơn:

```bash
python3 -m pip install --upgrade pip
python3 -m pip install requests lxml
```

## Cấu hình

Tạo hoặc chỉnh file `config.json` cùng thư mục với `translate.py`.

Ví dụ:

```json
{
  "source_language": "en",
  "target_languages": ["vi", "fr", "de", "pt-BR"]
}
```

### Ý nghĩa

- `source_language`: ngôn ngữ gốc của file `strings.xml`
- `target_languages`: danh sách ngôn ngữ cần dịch

## Cách dùng

### Chạy mặc định

```bash
python3 translate.py
```

### Bỏ qua các mục đã dịch

```bash
python3 translate.py --skip-translated
```

### Chỉnh số luồng dịch song song

```bash
python3 translate.py --skip-translated --workers 12
```

## Cách tool hoạt động

1. Tự động tìm file nguồn `strings.xml`
2. Đọc các resource có thể dịch từ `values/strings.xml`
3. Bỏ qua các mục `translatable="false"`
4. Dịch sang từng ngôn ngữ trong `config.json`
5. Tạo file output trong đúng thư mục `values-*`
6. Nếu dùng `--skip-translated`, các mục đã có bản dịch sẽ được giữ nguyên

## Output

Ví dụ file nguồn:

```text
app/src/main/res/values/strings.xml
```

Sau khi chạy, tool sẽ sinh các file như:

```text
app/src/main/res/values-vi/strings.xml
app/src/main/res/values-fr/strings.xml
app/src/main/res/values-de/strings.xml
app/src/main/res/values-pt-rBR/strings.xml
```

## Ví dụ tiến trình

```text
🌍 Đang dịch sang: vi
↷ Bỏ qua 12 mục đã có bản dịch
📝 Cần dịch 37 mục cho vi
[1/37] string::app_name
[2/37] string::welcome_message
[3/37] plural::deleted_files::one
...
✔ Xuất file: /path/to/app/src/main/res/values-vi/strings.xml
```

## Hỗ trợ resource

Tool hiện hỗ trợ:

- `string`
- `plurals`
- `string-array`

## Placeholder và format được bảo vệ

Tool cố gắng tránh làm hỏng các token quan trọng trong Android string như:

- printf placeholder:
  - `%s`
  - `%d`
  - `%1$s`
  - `%1$.2f`
  - `%%`
- braced placeholder:
  - `{name}`
  - `{count}`
- Android reference:
  - `@string/app_name`
  - `?attr/colorPrimary`
- escape sequence:
  - `\n`
  - `\t`
  - `\'`
  - `\"`
- XML/markup:
  - `<b>`
  - `<i>`
  - `<u>`
  - `<font>`
  - `<xliff:g>`

## Lưu ý

- Tool dùng Google Translate free endpoint nên có thể bị giới hạn hoặc lỗi tạm thời nếu gửi quá nhiều request
- Bản dịch tự động nên được kiểm tra lại với các câu có ngữ cảnh đặc biệt
- Với XML phức tạp, bản dùng `lxml` sẽ giữ cấu trúc tốt hơn bản dùng `xml.etree`
- Nếu một mục đã có bản dịch nhưng thực ra chưa đúng, hãy tắt `--skip-translated` để dịch lại

## Hạn chế hiện tại

- Không đảm bảo bản dịch luôn chính xác về ngữ cảnh
- Một số XML quá đặc biệt vẫn nên review thủ công sau khi dịch
- Google free endpoint không phải API chính thức cho production workflow lớn

## Gợi ý workflow

1. Giữ file gốc chuẩn trong `values/strings.xml`
2. Chạy tool để generate các bản dịch
3. Review lại các ngôn ngữ quan trọng
4. Commit các file `values-*` vào project

## License

MIT
