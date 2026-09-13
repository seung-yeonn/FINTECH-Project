from flask import Flask, request, jsonify
import os
import json
from datetime import datetime
from werkzeug.utils import secure_filename

from medical_encryptor import encrypt_medical_data
from key_manager import generate_hospital_keys, encrypt_aes_key
from IPFS_upload import upload_file_to_pinata
from blockchain_registry import register_file_on_chain
from ai_advisor import (
    fetch_dur_taboo_info,
    generate_medication_explanation,
    build_health_stats,
    generate_health_report,
)

app = Flask(__name__)

# ---------------------------------------------------------
# 기본 설정
# ---------------------------------------------------------
UPLOAD_FOLDER = 'uploads'          # 원본(평문 JSON) 임시 저장소 - 처리 후 즉시 삭제
ENCRYPTED_FOLDER = 'encrypted'     # AES-256-GCM으로 암호화된 의료데이터 저장소
KEYS_FOLDER = 'keys'                # 파일별 RSA 암호화된 AES 키 저장소 (평문 키는 저장 안 함)
REGISTRY_FILE = 'registry.json'    # 조회용 로컬 캐시

# key_manager.py가 고정 경로로 사용하는 병원 RSA 키 파일 (현재 병원 구분 없이 전역 1개)
HOSPITAL_PUBLIC_KEY_PATH = 'hospital_public.pem'

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 업로드 최대 16MB 제한

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(ENCRYPTED_FOLDER, exist_ok=True)
os.makedirs(KEYS_FOLDER, exist_ok=True)

ALLOWED_EXTENSIONS = {'json'}


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def ensure_hospital_keys():
    """
    key_manager.py는 hospital_public.pem / hospital_private.pem을
    현재 작업 디렉토리에 고정된 파일명으로 읽고 쓴다 (병원별 분리는 아직 미지원).
    없으면 최초 1회 생성한다.
    """
    if not os.path.exists(HOSPITAL_PUBLIC_KEY_PATH):
        generate_hospital_keys()


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
# 의료데이터 업로드:
# JSON 파싱 -> AES-256-GCM 암호화 -> AES키 RSA 암호화
# -> IPFS 업로드 -> 블록체인 등록
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

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f"{timestamp}_{secure_filename(file.filename)}"
    plain_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)

    # 1. 원본 JSON 임시 저장
    try:
        file.save(plain_path)
    except Exception as e:
        return jsonify({"error": f"파일 저장 중 오류가 발생했습니다: {str(e)}"}), 500

    # 2. JSON 파싱 (medical_encryptor.encrypt_medical_data는 dict를 받는다 - 파일 경로 아님)
    try:
        with open(plain_path, 'r', encoding='utf-8') as f:
            medical_data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        os.remove(plain_path)
        return jsonify({"error": f"올바른 JSON 형식이 아닙니다: {str(e)}"}), 400

    try:
        # 3. 병원 RSA 키 준비 (없으면 최초 1회 생성)
        ensure_hospital_keys()

        # 4. 의료데이터 AES-256-GCM 암호화 (요청마다 새 AES 키)
        try:
            encrypted_data, aes_key, file_hash = encrypt_medical_data(medical_data)
        except Exception as e:
            return jsonify({"error": f"의료데이터 암호화에 실패했습니다: {str(e)}"}), 500

        encrypted_filename = filename + '.encrypted'
        encrypted_path = os.path.join(ENCRYPTED_FOLDER, encrypted_filename)
        with open(encrypted_path, 'wb') as f:
            f.write(encrypted_data)

        # 5. AES 키를 병원 RSA 공개키로 암호화 (평문 aes_key는 디스크에 남기지 않는다)
        encrypted_key_path = os.path.join(KEYS_FOLDER, filename + '.enckey')
        try:
            encrypt_aes_key(aes_key, output_file=encrypted_key_path)
        except FileNotFoundError:
            return jsonify({"error": "병원 공개키(hospital_public.pem)를 찾을 수 없습니다."}), 500
        finally:
            aes_key = None  # 메모리에서도 참조를 최대한 빨리 제거

        # 6. IPFS(Pinata) 업로드 - 암호화된 의료데이터 파일만 업로드
        cid = upload_file_to_pinata(encrypted_path, name=encrypted_filename)
        if not cid:
            return jsonify({
                "error": "IPFS 업로드에 실패했습니다. PINATA_JWT 환경변수를 확인하세요.",
                "filename": filename
            }), 502

        # 7. 블록체인 등록
        try:
            chain_result = register_file_on_chain(cid, filename)
        except RuntimeError as e:
            # 블록체인 환경변수 미설정 등 - 암호화/IPFS는 이미 성공했으므로 그 결과는 살려서 반환
            return jsonify({
                "message": "암호화 및 IPFS 업로드는 성공했지만, 블록체인 등록에 실패했습니다.",
                "filename": filename,
                "cid": cid,
                "file_hash": file_hash,
                "blockchain_error": str(e)
            }), 207  # Multi-Status

        # 8. 조회용 로컬 레지스트리에 기록 (평문 AES 키는 절대 기록하지 않는다)
        registry = load_registry()
        registry.append({
            "filename": filename,
            "cid": cid,
            "file_hash": file_hash,
            "encrypted_key_path": encrypted_key_path,
            "tx_hash": chain_result["tx_hash"],
            "owner_address": chain_result["owner_address"],
            "uploaded_at": timestamp
        })
        save_registry(registry)

        return jsonify({
            "message": "업로드 → AES-256-GCM 암호화 → AES키 RSA 암호화 → IPFS 업로드 → 블록체인 등록 완료",
            "filename": filename,
            "cid": cid,
            "file_hash": file_hash,
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


# ---------------------------------------------------------
# 기능 A: 복약 설명 AI
# 흐름: 처방 약물명 -> DUR API(공식 사실) -> LLM(쉬운 말로 설명)
# ---------------------------------------------------------
@app.route('/medication-info', methods=['POST'])
def medication_info():
    payload = request.get_json(silent=True) or {}

    # 개인정보 보호: 약물명/진단명만 받는다. 환자 이름·ID 등은 이 엔드포인트로
    # 애초에 넘기지 않도록 프론트/호출부에서 걸러야 한다.
    drug_name = payload.get('prescription')
    diagnosis = payload.get('diagnosis')  # 선택

    if not drug_name:
        return jsonify({"error": "약물명(prescription)이 필요합니다."}), 400

    # 1. 공식 DUR 데이터 조회 (사실 확보)
    try:
        dur_data = fetch_dur_taboo_info(drug_name)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500

    # 2. LLM으로 쉬운 말 설명 생성 (설명만 담당, 사실은 새로 만들지 않음)
    try:
        explanation = generate_medication_explanation(drug_name, dur_data, diagnosis)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": f"AI 설명 생성 중 오류가 발생했습니다: {str(e)}"}), 502

    return jsonify({
        "drug_name": drug_name,
        "dur_data": dur_data,          # 원본 공식 데이터 (카드 하단 근거 표시용으로 활용 가능)
        "explanation": explanation,     # LLM이 생성한 쉬운 설명 (효능/복용법/주의사항/병용금기)
        "disclaimer": "이 정보는 참고용이며 정확한 복용법은 의료진·약사와 상담하세요."
    }), 200


# ---------------------------------------------------------
# 기능 B: 건강 통계 리포트 AI
# 흐름: 여러 visit 기록 -> pandas 통계(추세/변화율/이상치) -> LLM(자연어 리포트)
# ---------------------------------------------------------
@app.route('/health-report', methods=['POST'])
def health_report():
    payload = request.get_json(silent=True) or {}

    # visits 예시:
    # [{"date": "2026-06-01", "systolic": 128, "diastolic": 82, "glucose": 95}, ...]
    visits = payload.get('visits')

    # lifestyle 예시 (스키마 확장분, 가은님과 논의 필요):
    # {"exercise_frequency": "주 2회", "rehab_status": "재활 진행 중"}
    lifestyle = payload.get('lifestyle')

    if not visits or not isinstance(visits, list):
        return jsonify({"error": "visits(방문 기록 배열)가 필요합니다."}), 400

    # 1. pandas로 통계 처리 (사실 확보 - LLM은 이 숫자를 벗어난 값을 말하면 안 됨)
    try:
        stats = build_health_stats(visits)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"통계 처리 중 오류가 발생했습니다: {str(e)}"}), 500

    # 2. LLM으로 자연어 리포트 생성 (통계 요약값만 전달, 원본 방문기록/개인정보는 전달 안 함)
    try:
        report_text = generate_health_report(stats, lifestyle)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": f"AI 리포트 생성 중 오류가 발생했습니다: {str(e)}"}), 502

    return jsonify({
        "stats": stats,
        "report": report_text,
        "disclaimer": "이 리포트는 참고용이며 정확한 진단과 치료는 반드시 의료진과 상담하세요."
    }), 200


if __name__ == '__main__':
    app.run(debug=True, port=5000)
