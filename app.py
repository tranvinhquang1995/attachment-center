import streamlit as st
import os
import io
import re
import tempfile
import subprocess
from PIL import Image
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# Cấu hình trang Streamlit
st.set_page_config(
    page_title="Attachment Center - Tester Tools",
    page_icon="📁",
    layout="centered"
)

# --- KHAI BÁO BẬT/TẮT TÍNH NĂNG GOOGLE DRIVE ---
# BẠN CÓ THỂ ĐỔI GIÁ TRỊ NÀY THÀNH True KHI ĐÃ SẴN SÀNG KÍCH HOẠT LẠI TÍNH NĂNG UPLOAD GOOGLE DRIVE
GDRIVE_ENABLED = False 

# --- TIÊU ĐỀ ỨNG DỤNG ---
st.title("📁 Attachment Center (v3)")
st.markdown("Công cụ tối ưu hóa kích thước hình ảnh/video dành cho Tester.")
st.markdown("---")

# --- HẰNG SỐ & ĐỊNH CẤU HÌNH ---
# ID thư mục mẹ trên Google Drive (Mọi thư mục con như 109, 110 sẽ được tạo ở đây)
PARENT_FOLDER_ID = st.secrets.get("gdrive", {}).get("parent_folder_id", "YOUR_GOOGLE_DRIVE_PARENT_FOLDER_ID")

# --- HÀM KHỞI TẠO GOOGLE DRIVE SERVICE ---
@st.cache_resource
def get_gdrive_service():
    """Khởi tạo Drive API Service sử dụng Google Service Account từ Secrets"""
    try:
        gcp_info = st.secrets["gcp_service_account"]
        creds = service_account.Credentials.from_service_account_info(
            gcp_info,
            scopes=["https://www.googleapis.com/auth/drive"]
        )
        service = build("drive", "v3", credentials=creds)
        return service
    except Exception as e:
        st.error(f"❌ Không thể cấu hình Google Drive API. Vui lòng kiểm tra st.secrets. Lỗi: {e}")
        return None

# --- CÁC HÀM XỬ LÝ GOOGLE DRIVE ---
def find_or_create_folder(service, folder_name, parent_id):
    """Tìm thư mục con bằng tên dưới thư mục mẹ. Nếu chưa có thì tạo mới."""
    query = f"name = '{folder_name}' and '{parent_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    try:
        results = service.files().list(q=query, fields="files(id, name)").execute()
        items = results.get("files", [])
        
        if items:
            return items[0]["id"], False
        else:
            file_metadata = {
                "name": folder_name,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [parent_id]
            }
            folder = service.files().create(body=file_metadata, fields="id").execute()
            return folder.get("id"), True
    except Exception as e:
        st.error(f"❌ Lỗi khi tìm/tạo thư mục '{folder_name}': {e}")
        return None, False

def upload_file_to_drive(service, file_bytes, filename, mime_type, folder_id):
    """Upload file từ bộ nhớ lên thư mục chỉ định trên Google Drive"""
    file_metadata = {
        "name": filename,
        "parents": [folder_id]
    }
    media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype=mime_type, resumable=True)
    try:
        file = service.files().create(
            body=file_metadata,
            media_body=media,
            fields="id, name, webViewLink"
        ).execute()
        
        try:
            user_permission = {
                "type": "anyone",
                "role": "reader",
            }
            service.permissions().create(
                fileId=file.get("id"),
                body=user_permission,
                fields="id"
            ).execute()
        except Exception:
            pass
            
        return file.get("webViewLink")
    except Exception as e:
        st.error(f"❌ Lỗi trong quá trình upload lên Google Drive: {e}")
        return None

# --- CÁC HÀM NÉN HÌNH ẢNH & VIDEO ---
def compress_image(image_bytes, file_ext, target_size_mb=10.0):
    """Giảm chất lượng hình ảnh về dưới mức dung lượng mục tiêu (10MB)"""
    target_size_bytes = target_size_mb * 1024 * 1024
    if len(image_bytes) <= target_size_bytes:
        return image_bytes, False

    img = Image.open(io.BytesIO(image_bytes))
    
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
        
    quality = 90
    while quality >= 15:
        out_buf = io.BytesIO()
        img.save(out_buf, format="JPEG", quality=quality)
        compressed_bytes = out_buf.getvalue()
        if len(compressed_bytes) <= target_size_bytes:
            ratio = (len(image_bytes) - len(compressed_bytes)) / len(image_bytes)
            return compressed_bytes, (ratio >= 0.90)
        quality -= 5
        
    width, height = img.size
    scale = 0.9
    while scale >= 0.1:
        new_w, new_h = int(width * scale), int(height * scale)
        img_resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        out_buf = io.BytesIO()
        img_resized.save(out_buf, format="JPEG", quality=30)
        compressed_bytes = out_buf.getvalue()
        if len(compressed_bytes) <= target_size_bytes:
            ratio = (len(image_bytes) - len(compressed_bytes)) / len(image_bytes)
            return compressed_bytes, (ratio >= 0.90)
        scale -= 0.1
        
    return compressed_bytes, True

def get_video_duration(input_path):
    """Sử dụng ffprobe để lấy thời lượng video phục vụ tính toán bitrate"""
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", input_path
    ]
    try:
        output = subprocess.check_output(cmd).decode().strip()
        return float(output)
    except Exception:
        return None

def compress_video(video_bytes, target_size_mb=10.0):
    """Nén video về dưới 10MB bằng cách tự động tính toán bitrate và dùng ffmpeg"""
    target_size_bytes = target_size_mb * 1024 * 1024
    if len(video_bytes) <= target_size_bytes:
        return video_bytes, False
        
    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as in_file:
        in_file.write(video_bytes)
        in_path = in_file.name
        
    out_path = in_path + "_compressed.mp4"
    
    try:
        duration = get_video_duration(in_path)
        if duration and duration > 0:
            target_bits = 9.5 * 1024 * 1024 * 8
            total_bitrate = target_bits / duration
            
            audio_bitrate = 96000
            video_bitrate = max(100000, total_bitrate - audio_bitrate)
            
            cmd = [
                "ffmpeg", "-y", "-i", in_path,
                "-b:v", f"{int(video_bitrate)}",
                "-maxrate", f"{int(video_bitrate * 1.5)}",
                "-bufsize", f"{int(video_bitrate * 2)}",
                "-vcodec", "libx264",
                "-preset", "fast",
                "-acodec", "aac",
                "-b:a", f"{int(audio_bitrate)}",
                "-fs", "9.8M",
                out_path
            ]
        else:
            cmd = [
                "ffmpeg", "-y", "-i", in_path,
                "-vcodec", "libx264",
                "-crf", "30",
                "-preset", "fast",
                "-acodec", "aac",
                "-b:a", "96k",
                "-fs", "9.5M",
                out_path
            ]
            
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            with open(out_path, "rb") as f:
                compressed_bytes = f.read()
            
            ratio = (len(video_bytes) - len(compressed_bytes)) / len(video_bytes)
            return compressed_bytes, (ratio >= 0.90)
        else:
            return video_bytes, False
            
    finally:
        if os.path.exists(in_path):
            os.remove(in_path)
        if os.path.exists(out_path):
            os.remove(out_path)

# --- KHỞI CHẠY DRIVE SERVICE (Chỉ chạy khi tính năng được bật) ---
service = None
if GDRIVE_ENABLED:
    service = get_gdrive_service()

# --- GIAO DIỆN CHÍNH ---
st.subheader("📤 Upload Attachment & Lựa chọn chức năng")
uploaded_file = st.file_uploader(
    "Kéo thả hoặc chọn file hình ảnh/video của bạn",
    type=["png", "jpg", "jpeg", "mp4", "mov", "avi", "mkv"]
)

if uploaded_file is not None:
    file_bytes = uploaded_file.getvalue()
    file_name = uploaded_file.name
    file_size_mb = len(file_bytes) / (1024 * 1024)
    mime_type = uploaded_file.type
    
    is_image = mime_type.startswith("image/")
    is_video = mime_type.startswith("video/")
    
    st.info(f"📁 **Tập tin đã chọn:** `{file_name}` | Dung lượng gốc: **{file_size_mb:.2f} MB**")
    
    # --- QUẢN LÝ TRẠNG THÁI STREAMLIT (SESSION STATE) ---
    # Reset trạng thái khi upload file mới khác với file cũ
    if "prev_file_name" not in st.session_state or st.session_state.prev_file_name != file_name:
        st.session_state.prev_file_name = file_name
        st.session_state.compressed_bytes = None
        st.session_state.needs_warning = False
        st.session_state.was_compressed = False
        st.session_state.show_upload_form = False
        st.session_state.upload_success = False
        st.session_state.drive_link = None
        st.session_state.compression_message = ""

    # Thiết lập layout cho 2 nút chức năng chính thực hiện độc lập
    col_btn1, col_btn2 = st.columns(2)
    
    with col_btn1:
        st.markdown("### ⚡ Tính năng 1")
        compress_clicked = st.button("🗜️ Compress (Giảm dung lượng)", use_container_width=True)
        st.caption("Nén file dưới 10MB và tạo liên kết tải file tạm.")
        
    with col_btn2:
        st.markdown("### ☁️ Tính năng 2")
        if GDRIVE_ENABLED:
            upload_clicked = st.button("🚀 Upload (Tải lên Google Drive)", use_container_width=True)
            st.caption("Mở bảng sửa tên theo chuẩn và upload trực tiếp lên Drive.")
        else:
            upload_clicked = st.button("🚀 Upload (Google Drive) [In-process]", disabled=True, use_container_width=True)
            st.caption("⏳ *Tính năng tạm khóa - Đang chờ kiểm tra cấp quyền (In-process)*")

    # --- XỬ LÝ SỰ KIỆN: COMPRESS CLICKED ---
    if compress_clicked:
        st.session_state.was_compressed = True
        if file_size_mb > 10.0:
            with st.spinner("⚡ Đang thực hiện nén tối ưu tập tin dưới 10MB..."):
                if is_image:
                    compressed, warning = compress_image(file_bytes, file_name)
                    st.session_state.compressed_bytes = compressed
                    st.session_state.needs_warning = warning
                elif is_video:
                    compressed, warning = compress_video(file_bytes)
                    st.session_state.compressed_bytes = compressed
                    st.session_state.needs_warning = warning
                else:
                    st.session_state.compressed_bytes = file_bytes
                    st.session_state.needs_warning = False
                    st.warning("⚠️ Định dạng file này không hỗ trợ nén. Sẽ giữ nguyên file gốc.")
            
            comp_size_mb = len(st.session_state.compressed_bytes) / (1024 * 1024)
            st.session_state.compression_message = f"✅ Đã nén thành công! Dung lượng mới: **{comp_size_mb:.2f} MB**"
        else:
            st.session_state.compressed_bytes = file_bytes
            st.session_state.needs_warning = False
            st.session_state.compression_message = "✨ Dung lượng gốc dưới 10MB, không cần nén. Sẵn sàng tải xuống!"

    # --- HIỂN THỊ KẾT QUẢ NÉN (NẾU ĐÃ CLICK COMPRESS) ---
    if st.session_state.was_compressed and st.session_state.compressed_bytes is not None:
        st.markdown("---")
        st.subheader("📥 Tải File Tạm Thời (Chất lượng đã điều chỉnh)")
        
        if st.session_state.compression_message:
            st.success(st.session_state.compression_message)
            
        st.download_button(
            label="📥 Click để tải File Tạm về máy",
            data=st.session_state.compressed_bytes,
            file_name=f"compressed_{file_name}" if (file_size_mb > 10.0) else file_name,
            mime=mime_type,
            key="download_temp_btn"
        )
        
        # Cảnh báo nhỏ nếu dung lượng phải nén giảm vượt mức 90%
        if st.session_state.needs_warning:
            st.markdown(
                "<p style='color: #ff9800; font-size: 13px; margin-top: -10px; font-weight: 500;'>"
                "⚠️ Chất lượng file có thể bị ảnh hưởng nhiều do dung lượng file lớn"
                "</p>",
                unsafe_allow_html=True
            )

    # --- XỬ LÝ SỰ KIỆN: UPLOAD CLICKED ---
    if GDRIVE_ENABLED and upload_clicked:
        st.session_state.show_upload_form = True

    # --- HIỂN THỊ FORM UPLOAD (NẾU ĐÃ CLICK UPLOAD) ---
    if GDRIVE_ENABLED and st.session_state.show_upload_form:
        st.markdown("---")
        st.subheader("📝 Popup: Sửa tên & Xác nhận upload Google Drive")
        st.write("Hệ thống áp dụng **Phương án 2**: Tự động nhận diện thư mục dựa trên cấu trúc đặt tên file.")
        
        # Tách tên file gốc để lấy thông tin gợi ý ban đầu
        name_without_ext, ext = os.path.splitext(file_name)
        match = re.match(r"^([^-]+)-(.*)$", name_without_ext)
        
        if match:
            suggested_folder = match.group(1).strip()
            suggested_name = match.group(2).strip()
        else:
            suggested_folder = "Chung"
            suggested_name = name_without_ext

        # Form cho phép Tester tùy chỉnh thông tin trước khi upload
        with st.form("upload_form"):
            col_folder, col_name = st.columns(2)
            with col_folder:
                folder_input = st.text_input("📁 Tên Thư mục đích (Ví dụ: 109):", value=suggested_folder)
            with col_name:
                filename_input = st.text_input("📝 Tên Attachment (không kèm đuôi file):", value=suggested_name)
                
            final_filename = f"{folder_input}-{filename_input}{ext}"
            
            # Xác định phiên bản file sẽ được upload lên Drive:
            has_compressed_version = (st.session_state.compressed_bytes is not None and st.session_state.was_compressed)
            
            st.markdown("**🔍 Xem trước kết quả xử lý:**")
            st.markdown(f"- **Thư mục lưu trữ:** `Root / {folder_input}/` (Sẽ tự động tìm hoặc tạo nếu chưa có)")
            st.markdown(f"- **Tên file sẽ lưu:** `{final_filename}`")
            
            if has_compressed_version and file_size_mb > 10.0:
                st.warning("⚡ **Phiên bản sẽ upload:** File đã được giảm dung lượng (< 10MB)")
                upload_data = st.session_state.compressed_bytes
            else:
                st.info("ℹ️ **Phiên bản sẽ upload:** File gốc (Chưa nén)")
                upload_data = file_bytes
            
            submit_btn = st.form_submit_button("🚀 Xác nhận Upload lên Google Drive")
            
        if submit_btn:
            if PARENT_FOLDER_ID == "YOUR_GOOGLE_DRIVE_PARENT_FOLDER_ID":
                st.error("❌ Vui lòng thiết lập ID thư mục mẹ (PARENT_FOLDER_ID) trong file secrets.")
            elif service is None:
                st.error("❌ Google Drive service chưa được khởi chạy thành công. Vui lòng kiểm tra cấu hình secrets.")
            else:
                with st.spinner("⏳ Đang xử lý tạo thư mục và tải file lên Google Drive..."):
                    # 1. Tìm hoặc tạo thư mục con
                    folder_id, created_new = find_or_create_folder(service, folder_input, PARENT_FOLDER_ID)
                    
                    if folder_id:
                        if created_new:
                            st.info(f"📂 Đã tự động tạo mới thư mục `{folder_input}` trên Google Drive.")
                        else:
                            st.info(f"📂 Đã tìm thấy thư mục `{folder_input}` sẵn có trên Google Drive.")
                            
                        # 2. Upload file lên thư mục con đó
                        drive_link = upload_file_to_drive(
                            service=service,
                            file_bytes=upload_data,
                            filename=final_filename,
                            mime_type=mime_type,
                            folder_id=folder_id
                        )
                        
                        if drive_link:
                            st.session_state.upload_success = True
                            st.session_state.drive_link = drive_link
                        else:
                            st.error("❌ Upload thất bại.")
                    else:
                        st.error("❌ Không thể xác định/tạo thư mục đích trên Google Drive.")
                        
        if st.session_state.upload_success and st.session_state.drive_link:
            st.balloons()
            st.success("🎉 Upload thành công lên Google Drive!")
            st.markdown("### 🔗 Đường dẫn file đã tải lên:")
            st.code(st.session_state.drive_link, language="markdown")
            st.info("💡 Click vào biểu tượng Copy ở góc phải của dòng code trên để sao chép nhanh.")

# Hiển thị chú thích cấu hình nếu tính năng được bật
if GDRIVE_ENABLED and service is None:
    st.info("💡 Hướng dẫn cấu hình st.secrets nằm trong file README-v3.md.")
