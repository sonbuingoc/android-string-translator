# android-string-translator

Tool Python dịch **strings.xml** cho Android project — **full tự động**.

✅ Auto detect `strings.xml`  
✅ Bỏ qua `translatable="false"`  
✅ Escape chuẩn Android (`& < > \'`)  
✅ Tạo đúng thư mục `values-xx`, `values-xx-rYY`  
✅ Dịch song song (rất nhanh)  
✅ Không phụ thuộc Android Studio

---

## 📦 Requirements

- macOS / Linux / Windows
- **Python ≥ 3.8**
- Internet (sử dụng Google Translate free endpoint)

---

## 🚀 Usage

### 1️⃣ Cài Python dependency (bắt buộc)

```bash
python3 -m pip install --upgrade pip
python3 -m pip install requests
```

### 2️⃣ Thêm tool vào project (dùng như submodule hoặc clone trực tiếp)
```bash
git clone https://github.com/sonbuingoc/android-string-translator.git
```

### 3️⃣ Chạy tool
```bash
cd android-string-translator
python3 translate.py
```