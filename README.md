# android-string-translator

Công cụ Python giúp dịch tự động Android resource XML cho Android project.

Tool này phù hợp khi bạn muốn tạo nhanh các file đa ngôn ngữ từ `values/strings.xml` hoặc `values/arrays.xml` mà không cần thao tác thủ công trong Android Studio.

## Tính năng

- Tự động tìm file resource nguồn, mặc định là `strings.xml`
- Bỏ qua các resource có `translatable="false"`
- Hỗ trợ:
  - `string`
  - `plurals`
  - `string-array`
- Tạo đúng thư mục Android resource:
  - `values-fr`
  - `values-vi`
  - `values-pt-rBR`
- Có 3 translation engine: Google GTX, Argos Translate local, NLLB-200 local
- Dịch song song theo số `Workers` cấu hình cho Google, Argos và NLLB
- Hiển thị tiến trình theo từng ngôn ngữ, ví dụ `1/37`
- Có option bỏ qua các mục đã dịch với `--skip-translated`
- Có option chỉ dịch các id được chỉ định với `--ids`
- Có option dịch từng từ riêng lẻ với `--word-by-word`
- Có option chỉ định Android project cần dịch với `--project-root`
- Có option chỉ định file resource cần dịch với `--resource-file`
- Có option dịch cùng resource file trong tất cả module với `--all-modules`
- Bảo vệ placeholder và format Android tốt hơn:
  - `%s`, `%1$s`, `%d`, `%1$.2f`, `%%`
  - `{name}`, `{count}`
  - `\n`, `\t`, `\'`, `\"`
  - `@string/...`, `?attr/...`
  - tag như `<b>`, `<i>`, `<u>`, `<xliff:g>`
- Không phụ thuộc Android Studio

## Requirements

- macOS / Linux / Windows
- Python 3.11 khuyến nghị/bắt buộc cho setup local engine trên macOS
- Internet connection chỉ bắt buộc khi dùng Google hoặc lần đầu tải model/package local
- Google Translate free endpoint, Argos Translate hoặc NLLB-200

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

Cài đầy đủ cả Argos và NLLB bằng virtual environment riêng:

```bash
brew install python@3.11
bash setup_local_engines.sh
```

Script sẽ tạo `.venv` và GUI sẽ tự dùng `.venv/bin/python3` khi có.

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

- `source_language`: ngôn ngữ gốc của file resource nguồn
- `target_languages`: danh sách ngôn ngữ cần dịch

## Cách dùng

### Chạy mặc định

```bash
python3 translate.py
```

CLI mặc định vẫn dùng Google để tương thích với cách chạy cũ. Chọn local engine bằng:

```bash
python3 translate.py --engine argos
python3 translate.py --engine nllb
```

Argos sẽ tự tải language package còn thiếu. NLLB sử dụng model `facebook/nllb-200-distilled-600M`; lần đầu chạy sẽ tải model từ Hugging Face, các lần sau dùng cache trên máy.

Mặc định tool tìm Android project ở thư mục cha của repository này.

### Chạy giao diện

```bash
python3 gui.py
```

GUI cho phép chọn Android project folder rồi tự scan từng file `src/main/res/values/*.xml` có `string`, `plurals` hoặc `string-array`. Mỗi file vật lý theo từng module/path được hiển thị riêng để người dùng chọn. GUI hỗ trợ `Argos (local)`, `NLLB-200 (local)` hoặc `Google GTX`; Argos là lựa chọn mặc định. Local engine tự tải model/package ở lần đầu và dùng cache local cho các lần sau. GUI cũng hỗ trợ `Skip translated`, `Word by word`, `Workers`, `IDs`, `source_language` và `target_languages`.

GUI chạy bằng local web server và tự mở browser ở `http://127.0.0.1:<port>`. Trên macOS, nút `Choose Folder` sẽ mở folder picker native; trên hệ khác có thể paste đường dẫn project thủ công.

### Chỉ định Android project root

```bash
python3 translate.py --project-root /path/to/android-project
```

Đường dẫn tương đối cũng được hỗ trợ và được tính từ thư mục đang chạy lệnh:

```bash
python3 translate.py --project-root ../my-android-app --skip-translated
```

### Dịch arrays.xml

```bash
python3 translate.py --resource-file arrays.xml --skip-translated
```

Với `string-array`, tool chỉ dịch text bên trong từng thẻ `<item>` và giữ nguyên resource name:

```xml
<string-array name="daily_charge_notification_messages">
    <item>:zap: Ready? Watch paper come to life as art!</item>
    <item>:battery: Boost your vibe — a new origami design awaits you.</item>
</string-array>
```

### Dịch tất cả module

```bash
python3 translate.py --all-modules --skip-translated
```

Mặc định tool chỉ dịch một file nguồn, ưu tiên module `app`. Khi bật `--all-modules`, tool sẽ scan toàn bộ Android project và dịch mọi file khớp dạng:

```text
*/src/main/res/values/strings.xml
```

Có thể kết hợp với `--resource-file` để dịch file khác trong tất cả module:

```bash
python3 translate.py --all-modules --resource-file arrays.xml --skip-translated
```

Nên dùng kèm `--skip-translated` để tránh dịch lại toàn bộ resource và giảm nguy cơ bị Google Translate free endpoint giới hạn request.

### Bỏ qua các mục đã dịch

```bash
python3 translate.py --skip-translated
```

### Chỉnh số luồng dịch song song

```bash
python3 translate.py --skip-translated --workers 12
```

### Chỉ dịch các id chỉ định

```bash
python3 translate.py --ids app_name welcome_message
```

Hoặc dùng dấu phẩy:

```bash
python3 translate.py --ids app_name,welcome_message
```

Với `plurals` và `string-array`, truyền resource name để dịch toàn bộ item bên trong:

```bash
python3 translate.py --ids deleted_files onboarding_steps
```

Nếu cần chỉ định chính xác một item nội bộ, có thể dùng key đầy đủ mà tool in ra trong progress:

```bash
python3 translate.py --ids plural::deleted_files::one string-array::onboarding_steps::0
```

Khi dùng `--ids`, tool sẽ đọc file dịch hiện có và giữ nguyên các id không được chọn nếu file đích đã tồn tại.

### Dịch từng từ

```bash
python3 translate.py --word-by-word
```

Có thể kết hợp với `--ids` và `--skip-translated`:

```bash
python3 translate.py --ids app_name welcome_message --word-by-word --skip-translated
```

## Cách tool hoạt động

1. Tìm file nguồn trong project mặc định hoặc project được truyền qua `--project-root`
2. Đọc các resource có thể dịch từ `values/<resource-file>`
3. Bỏ qua các mục `translatable="false"`
4. Dịch sang từng ngôn ngữ trong `config.json`
5. Tạo file output trong đúng thư mục `values-*`
6. Nếu dùng `--ids`, chỉ các id được chọn được đưa vào hàng đợi dịch
7. Nếu dùng `--skip-translated`, các mục đã có bản dịch sẽ được giữ nguyên
8. Nếu dùng `--all-modules`, các bước trên được chạy cho từng module có file nguồn khớp

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

Nếu chạy với `--resource-file arrays.xml`, output sẽ giữ cùng tên file:

```text
app/src/main/res/values-vi/arrays.xml
app/src/main/res/values-fr/arrays.xml
```

## Ví dụ tiến trình

```text
== Module /path/to/app ==
OK    Loaded 49 translatable item(s) from /path/to/app/src/main/res/values/strings.xml

== Language [1/18] vi ==
INFO  Items: total=49, translate=37, skipped=12, workers=8
  [1/37] string::app_name
  [2/37] string::welcome_message
  [3/37] plural::deleted_files::one
...
OK    Wrote /path/to/app/src/main/res/values-vi/strings.xml

== Summary ==
OK    Modules processed: 1/1
OK    Languages processed: 18
OK    Items translated: 37
OK    Items skipped: 12
OK    Files written: 18
OK    Completed without translation errors.
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
- Nếu một item dịch lỗi, tool sẽ in `ERROR` theo language/resource key, giữ text gốc cho item đó, và tổng hợp lỗi ở cuối run
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
