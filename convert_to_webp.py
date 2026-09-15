"""
이미지 일괄 WebP 자동 변환 스크립트 (Pillow 활용)
- 프로젝트 내 assets/ 폴더의 모든 PNG/JPG 이미지를 고품질 WebP(품질 85%)로 일괄 변환합니다.
"""

import os
from PIL import Image

def convert_images_to_webp(target_dir='assets', quality=85):
    if not os.path.exists(target_dir):
        print(f"[ERROR] 디렉토리가 존재하지 않습니다: {target_dir}")
        return []

    converted_files = []
    print(f"=== WebP 일괄 변환 시작 (폴더: {target_dir}, 품질: {quality}%) ===")

    for fname in sorted(os.listdir(target_dir)):
        if fname.lower().endswith(('.png', '.jpg', '.jpeg')):
            src_path = os.path.join(target_dir, fname)
            base_name, _ = os.path.splitext(fname)
            webp_name = f"{base_name}.webp"
            webp_path = os.path.join(target_dir, webp_name)

            try:
                with Image.open(src_path) as img:
                    # RGBA/RGB 등 적절한 모드로 변환
                    if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
                        img = img.convert('RGBA')
                    else:
                        img = img.convert('RGB')

                    img.save(webp_path, 'WEBP', quality=quality, method=6)

                orig_size = os.path.getsize(src_path)
                webp_size = os.path.getsize(webp_path)
                ratio = (1 - webp_size / orig_size) * 100

                converted_files.append({
                    'original': fname,
                    'webp': webp_name,
                    'orig_size': orig_size,
                    'webp_size': webp_size,
                    'saved_ratio': ratio
                })

                print(f"  [변환 완료] {fname:28s} ({orig_size:>9,d} B) -> {webp_name:28s} ({webp_size:>7,d} B) | 절감율: {ratio:5.1f}%")

            except Exception as e:
                print(f"  [변환 실패] {fname}: {e}")

    print(f"=== 변환 완료: 총 {len(converted_files)}개 이미지 ===")
    return converted_files

if __name__ == '__main__':
    convert_images_to_webp()
