import os
from file_encryptor import load_key, encrypt_file
from IPFS_upload import upload_file_to_pinata
from 블록체인_등록 import register_file

def process_and_upload(file_path):
    print("=== 보안 파일 업로드 프로세스 시작 ===")

    # 1. 암호화 키 불러오기
    print("\n[1/3] 암호화 키 불러오는 중...")
    try:
        # 변경된 경로 적용! 👇
        my_key = load_key("secret.key") 
    except FileNotFoundError:
        print("❌ 오류: 키 파일이 없습니다.")
        return None

    # 2. 원본 파일 암호화
    print("\n[2/3] 파일 암호화 진행 중...")
    encrypted_file_path = encrypt_file(file_path, my_key)
    
    if not encrypted_file_path:
        print("❌ 암호화 실패.")
        return None

    # 3. 암호화된 파일을 IPFS에 업로드
    print("\n[3/3] IPFS에 안전하게 업로드하는 중...")
    # 원본이 아닌 '암호화된 파일(encrypted_file_path)'을 올려줍니다!
    cid = upload_file_to_pinata(encrypted_file_path)
    if cid:
            # 4. 블록체인에 CID 등록
            print("\n[4/4] 블록체인에 CID 등록 중...")
            try:
                result = register_file(cid, encrypted_file_path)
                print(f"   📋 등록 인덱스  : {result['index']}")
                print(f"   🔗 TX Hash     : {result['tx_hash']}")
                print(f"   👛 소유자 주소  : {result['owner']}")
            except Exception as e:
                print(f"❌ 블록체인 등록 실패: {e}")
                return None

            print("\n🎉 [최종 완료] 모든 과정이 성공적으로 끝났습니다!")
            print(f"🔒 보호된 파일    : {encrypted_file_path}")
            print(f"🌐 IPFS 주소(CID) : {cid}")
            print(f"⛓  블록체인 TX    : {result['tx_hash']}")
            return cid
    else:
            print("\n❌ IPFS 업로드 실패.")
            return None

# --- 테스트 실행 부분 ---
if __name__ == "__main__":
    # 변경된 경로 적용!
    target_file = "data/final_test_data.txt" 
    with open(target_file, "w", encoding="utf-8") as f:
        f.write("이 중요한 데이터는 암호화 과정을 거친 뒤에 IPFS 네트워크로 날아갈 것입니다.")
    
    process_and_upload(target_file)