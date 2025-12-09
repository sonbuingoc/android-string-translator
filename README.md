# android-string-translator

Tool Python dịch **strings.xml** cho Android project — full tự động.

✅ Auto detect strings.xml  
✅ Bỏ qua `translatable="false"`  
✅ Escape chuẩn Android (`& < > \'`)  
✅ Tạo đúng thư mục `values-xx`, `values-xx-rYY`  
✅ Dịch song song (rất nhanh)  
✅ Không phụ thuộc Android Studio

---

## 📦 Yêu cầu hệ thống

- macOS / Linux / Windows
- **Python ≥ 3.8**
- Internet (dùng Google Translate free endpoint)

---

## 🔧 Cài đặt Python dependencies

### ✅ BẮT BUỘC: cài thư viện `requests`

```bash
python3 -m pip install --upgrade pip
python3 -m pip install requests
```
## Thêm tool vào root project
```bash
git clone https://github.com/sonbuingoc/android-string-translator.git
```

## Sử dụng
```bash
cd android-string-translator
python3 translate.py
```

