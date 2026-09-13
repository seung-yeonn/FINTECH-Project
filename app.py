from flask import Flask, request, jsonify
import os
import json
from datetime import datetime
from werkzeug.utils import secure_filename

from file_encryptor import load_key, generate_key, encrypt_file
from IPFS_upload import upload_file_to_pinata
from blockchain_registry import register_file_on_chain

app = Flask(__name__)

# ---------------------------------------------------------
# 기본 설정
# ---------------------------------------------------------
UPLOAD_FOLDER = 'uploads'          # 원본(평문) 임시 저장소 - 처리 후 즉시 삭제
ENCRYPTED_FOLDER = 'encrypted'     # 암호화된 파일 저장소
KEY_PATH = 'keys/secret.key'
REGISTRY_FILE = 'registry.json'    # 조회용 로컬 캐시 (CID, 트랜잭션 해시 등)

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 업로드 최대 16MB 제한

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(ENCRYPTED_FOLDER, exist_ok=True)
os.makedirs('keys', exist_ok=True)

ALLOWED_EXTENSIONS = {'json'}


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def get_or_create_key():
    """keys/secret.key가 없으면 새로 생성하고, 있으면 그대로 불러온다."""
    if not os.path.exists(KEY_PATH):
        return generate_key(KEY_PATH)
    return load_key(KEY_PATH)


def load_registry():
    if not os.path.exists(REGISTRY_FILE):
        return []
    with open(REGISTRY_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_registry(entries):
    with open(REGISTRY_FILE, 'w', encoding='utf-8') as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------
# 헬스체크
# ---------------------------------------------------------
@app.route('/')
def index():
    return jsonify({"message": "Server is running."})


@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok"})


# ---------------------------------------------------------
# 파일 업로드: 암호화 -> IPFS 업로드 -> 블록체인 등록
# ---------------------------------------------------------
@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({"error": "요청에 파일이 포함되어 있지 않습니다."}), 400

    file = request.files['file']

    if file.filename == '':
        return jsonify({"error": "선택된 파일이 없습니다."}), 400

    if not allowed_file(file.filename):
        return jsonify({"error": "JSON 파일만 업로드 가능합니다."}), 400

    # 파일명 충돌 방지를 위해 타임스탬프 prefix 추가
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f"{timestamp}_{secure_filename(file.filename)}"
    plain_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)

    # 1. 원본 파일 임시 저장
    try:
        file.save(plain_path)
    except Exception as e:
        return jsonify({"error": f"파일 저장 중 오류가 발생했습니다: {str(e)}"}), 500

    # 1-1. JSON 유효성 검증 (확장자만 .json이고 내용이 깨진 경우 여기서 걸러냄)
    try:
        with open(plain_path, 'r', encoding='utf-8') as f:
            json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        os.remove(plain_path)
        return jsonify({"error": f"올바른 JSON 형식이 아닙니다: {str(e)}"}), 400

    try:
        # 2. 암호화 (Fernet)
        key = get_or_create_key()
        encrypted_path = encrypt_file(plain_path, key)
        if not encrypted_path:
            return jsonify({"error": "파일 암호화에 실패했습니다."}), 500

        final_encrypted_path = os.path.join(ENCRYPTED_FOLDER, os.path.basename(encrypted_path))
        os.replace(encrypted_path, final_encrypted_path)

        # 3. IPFS(Pinata) 업로드
        cid = upload_file_to_pinata(final_encrypted_path)
        if not cid:
            return jsonify({
                "error": "IPFS 업로드에 실패했습니다. PINATA_JWT 환경변수를 확인하세요.",
                "filename": filename
            }), 502

        # 4. 블록체인 등록
        try:
            chain_result = register_file_on_chain(cid, filename)
        except RuntimeError as e:
            # 블록체인 환경변수 미설정 등 - 암호화/IPFS는 이미 성공했으므로 그 결과는 살려서 반환
            return jsonify({
                "message": "암호화 및 IPFS 업로드는 성공했지만, 블록체인 등록에 실패했습니다.",
                "filename": filename,
                "cid": cid,
                "blockchain_error": str(e)
            }), 207  # Multi-Status

        # 5. 조회용 로컬 레지스트리에 기록
        registry = load_registry()
        registry.append({
            "filename": filename,
            "cid": cid,
            "tx_hash": chain_result["tx_hash"],
            "owner_address": chain_result["owner_address"],
            "uploaded_at": timestamp
        })
        save_registry(registry)

        return jsonify({
            "message": "파일 업로드 → 암호화 → IPFS 업로드 → 블록체인 등록 완료",
            "filename": filename,
            "cid": cid,
            "tx_hash": chain_result["tx_hash"],
            "owner_address": chain_result["owner_address"]
        }), 200

    finally:
        # 평문 원본은 서버에 남기지 않는다 (보안)
        if os.path.exists(plain_path):
            os.remove(plain_path)


@app.route('/files', methods=['GET'])
def list_files():
    registry = load_registry()
    return jsonify({
        "files": registry,
        "count": len(registry)
    })


if __name__ == '__main__':
    app.run(debug=True, port=5000)
