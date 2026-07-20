# 🔍 toolwatch 코드 리뷰 보고서

> 리뷰 도구: **Antigravity** (Google DeepMind)
> 리뷰 대상: `src/pi/` (client.py, hw.py, config.json) + `src/server/` (app.py, db.py, config.json, templates/)
> 리뷰 일시: 2026-07-15

---

## 총평

| 심각도 | Pi 클라이언트 | 서버 | 합계 |
|--------|:---:|:---:|:---:|
| 🔴 Critical | 3 | 5 | **8** |
| 🟡 Warning | 5 | 15 | **20** |
| 🔵 Info | 3 | 12 | **15** |

전체적으로 판정 로직의 순수 함수 분리, 디바운스/세션 설계, selfcheck 테스트 등 **설계 품질은 우수**하다. 주요 개선 포인트는 **보안(인증·비밀번호)**, **에러 처리(로깅·트랜잭션)**, **성능(추론 중 전역 락)** 세 축으로 집약된다.

---

## 🔴 Critical — 즉시 수정 필요

### C1. YOLO 추론이 전역 락 내부에서 실행
- **파일**: [app.py:243](file:///C:/Users/sungjun/Documents/임베디드시스템/src/server/app.py#L237-L306)
- **문제**: `detect_tools()` (수백 ms)가 `_state_lock` 안에서 실행되어, 추론 동안 대시보드 포함 모든 요청이 블로킹됨
- **수정**:
```diff
-    with _state_lock:
-        ...
-        detected_counts = detect_tools(frame_bytes)
-        ...
+    detected_counts = detect_tools(frame_bytes)  # 락 밖에서 추론
+    with _state_lock:
+        ...  # 결과만 갖고 상태 갱신
```

### C2. `/frame` 엔드포인트 무인증
- **파일**: [app.py:229-234](file:///C:/Users/sungjun/Documents/임베디드시스템/src/server/app.py#L229-L234)
- **문제**: 누구든 POST로 위조 이미지를 보내면 공구 반출/반납 이벤트를 조작 가능
- **수정**: API 키 헤더(`X-API-Key`) 비교 추가

### C3. `except Exception: pass` — 모든 예외 무시
- **파일**: [client.py:101-104](file:///C:/Users/sungjun/Documents/임베디드시스템/src/pi/client.py#L101-L104), [client.py:41-43](file:///C:/Users/sungjun/Documents/임베디드시스템/src/pi/client.py#L41-L43)
- **문제**: 카메라·네트워크·JSON 파싱 등 모든 오류를 조용히 삼킴. 설정 오류 같은 영구적 문제도 무한 재시도. 로그가 전혀 없어 디버깅 불가
- **수정**:
```diff
- except Exception:
-     pass
+ except requests.RequestException as e:
+     logging.warning("서버 통신 실패: %s", e)
+ except Exception as e:
+     logging.error("예상치 못한 오류: %s", e, exc_info=True)
```

### C4. 비밀번호·secret_key 평문 하드코딩
- **파일**: [server/config.json:21-22](file:///C:/Users/sungjun/Documents/임베디드시스템/src/server/config.json#L21-L22)
- **문제**: `"hmi_password": "toolwatch1234"`, `"secret_key": "toolwatch-dev-secret-change-me"` — Git 이력에 영구 노출, 세션 쿠키 위조 가능
- **수정**: 환경변수로 이관. secret_key는 `os.urandom(32).hex()`로 생성

### C5. DB 예외 시 부분 커밋 + 메모리 상태 불일치
- **파일**: [app.py:237-286](file:///C:/Users/sungjun/Documents/임베디드시스템/src/server/app.py#L237-L286)
- **문제**: 하나의 `/frame` 요청에서 여러 DB 함수가 각각 `commit()`을 호출. 중간 에러 시 부분 커밋 + 메모리 상태 불일치
- **수정**: DB 함수에서 개별 `commit()` 제거, 호출부에서 전체 성공 시 한 번만 `commit()`, 실패 시 `rollback()`

---

## 🟡 Warning — 조기 수정 권장

### Pi 클라이언트

| # | 파일:줄 | 이슈 | 수정 방향 |
|---|---------|------|-----------|
| W1 | [client.py:35](file:///C:/Users/sungjun/Documents/임베디드시스템/src/pi/client.py#L35) | RFID 스레드에서 `SimpleMFRC522()` 리소스 누수 — daemon thread 종료 시 GPIO cleanup 미호출 | 종료 플래그(`Event`) + `finally` 정리 |
| W2 | [client.py:100](file:///C:/Users/sungjun/Documents/임베디드시스템/src/pi/client.py#L100) | 서버 응답에 `light`/`buzzer` 키 없으면 `KeyError` — `except`에 삼켜져 원인 불명 | `result.get("light", "green")` 방어 |
| W3 | [client.py:83](file:///C:/Users/sungjun/Documents/임베디드시스템/src/pi/client.py#L83) | `requests.Session()`이 닫히지 않음 | `finally`에 `session.close()` 추가 |
| W4 | [hw.py:27-30](file:///C:/Users/sungjun/Documents/임베디드시스템/src/pi/hw.py#L27-L30) | 유효하지 않은 `color` 값 시 모든 LED 꺼짐, 오류 미보고 | 입력 검증 + 경고 로그 |
| W5 | [hw.py:16-23](file:///C:/Users/sungjun/Documents/임베디드시스템/src/pi/hw.py#L16-L23) | `init()`에서 필수 키(`red`/`yellow`/`green`/`buzzer`) 누락 검증 없음 | `REQUIRED_PINS` 세트 대조 |

### 서버

| # | 파일:줄 | 이슈 | 수정 방향 |
|---|---------|------|-----------|
| W6 | [db.py:44-47](file:///C:/Users/sungjun/Documents/임베디드시스템/src/server/db.py#L44-L47) | 커넥션에 context manager 미사용 — 누수 위험 | `contextlib.contextmanager` 래핑 |
| W7 | [db.py:22](file:///C:/Users/sungjun/Documents/임베디드시스템/src/server/db.py#L22) | FK 제약 사용하려면 `PRAGMA foreign_keys = ON` 필요. 현재 비활성 | `get_conn()`에서 PRAGMA 설정 |
| W8 | [db.py:76-78](file:///C:/Users/sungjun/Documents/임베디드시스템/src/server/db.py#L76-L78) | `delete_user`에서 연관 loan 미처리 → 고아 레코드 | 삭제 전 open loan 확인 |
| W9 | [app.py:234](file:///C:/Users/sungjun/Documents/임베디드시스템/src/server/app.py#L234) | 업로드 파일 크기 제한 없음 → OOM 가능 | `MAX_CONTENT_LENGTH` 설정 |
| W10 | [app.py:351](file:///C:/Users/sungjun/Documents/임베디드시스템/src/server/app.py#L351) | 비밀번호 평문 비교 (타이밍 공격 취약) | `check_password_hash()` 사용 |
| W11 | [app.py:183-188](file:///C:/Users/sungjun/Documents/임베디드시스템/src/server/app.py#L183-L188) | 같은 공구 2개가 동시 반출 시 스냅샷 파일명 충돌 | loan_id를 파일명에 포함 |
| W12 | [app.py:405-408](file:///C:/Users/sungjun/Documents/임베디드시스템/src/server/app.py#L405-L408) | 스냅샷 경로에 Path traversal 가능성 | `secure_filename()` 적용 |
| W13 | templates 전체 | CSRF 토큰 없음 — 모든 POST form이 CSRF 공격 노출 | Flask-WTF `CSRFProtect` 도입 |
| W14 | [dashboard.html:392](file:///C:/Users/sungjun/Documents/임베디드시스템/src/server/templates/dashboard.html#L392) | Base64 이미지 인라인 — 큰 이미지 시 HTML 수 MB, 5초마다 전체 전송 | 별도 `/latest_frame` 엔드포인트 분리 |
| W15 | [server/config.json:4](file:///C:/Users/sungjun/Documents/임베디드시스템/src/server/config.json#L4) | `confidence_threshold` 범위 검증 없음 | 로드 시 0 < threshold < 1 체크 |

---

## 🔵 Info — 개선 권장

| # | 위치 | 이슈 | 비고 |
|---|------|------|------|
| I1 | client.py 전체 | `logging` 모듈 미사용 — `print`도 없어 운영 중 상태 파악 불가 | `logging.basicConfig()` 추가 |
| I2 | [hw.py:12-13](file:///C:/Users/sungjun/Documents/임베디드시스템/src/pi/hw.py#L12-L13) | 모듈 수준 가변 상태 — 테스트 격리 어려움 | 클래스 래핑 검토 |
| I3 | pi/config.json | GPIO 핀이 BCM인지 BOARD인지 주석 없음 | 주석 추가 (gpiozero 기본 BCM) |
| I4 | [db.py:12-41](file:///C:/Users/sungjun/Documents/임베디드시스템/src/server/db.py#L12-L41) | `loans` 테이블에 `uid`, `returned_at` 인덱스 미정의 — 데이터 증가 시 성능 저하 | `CREATE INDEX` 추가 |
| I5 | [db.py:146-170](file:///C:/Users/sungjun/Documents/임베디드시스템/src/server/db.py#L146-L170) | `get_recent_events` 한글 딕셔너리 키(`"시각"`, `"이벤트"`) — 코드 전반에서 깨지기 쉬움 | 영문 키 → 프론트에서 매핑 |
| I6 | [app.py:191-192](file:///C:/Users/sungjun/Documents/임베디드시스템/src/server/app.py#L191-L192) | `datetime.now()`에 timezone 미지정 — Pi와 서버 시간대 불일치 가능 | KST 명시 또는 UTC 통일 |
| I7 | [app.py:215-217](file:///C:/Users/sungjun/Documents/임베디드시스템/src/server/app.py#L215-L217) | `save_config()`가 동시 요청 시 race condition | atomic write (임시파일 → rename) |
| I8 | [app.py:36-44](file:///C:/Users/sungjun/Documents/임베디드시스템/src/server/app.py#L36-L44) | 전역 mutable `state` dict — 테스트 격리·멀티프로세스 불가 | 클래스 캡슐화 |
| I9 | [app.py:677](file:///C:/Users/sungjun/Documents/임베디드시스템/src/server/app.py#L677) | `app.run()`으로 프로덕션 실행 — Flask 내장 서버는 개발용 | gunicorn/waitress 사용 |
| I10 | dashboard.html:296, 488 | "경고 해제"·"UID 삭제" 버튼에 확인 다이얼로그 없음 | `onclick="return confirm(…)"` |
| I11 | templates 전체 | CSS 210줄이 `<style>` 인라인, dashboard·student 간 중복 | `static/style.css` 분리 + `{% extends %}` |
| I12 | student.html:117 | `{{ name }}님` — name이 빈 문자열이면 "님"만 표시 | `{{ name or "알 수 없음" }}님` |

---

## 🏆 잘 된 점

- **판정 로직 순수 함수 분리** (`judge_tools`, `resolve_session`, `attribute_out`, `check_overdue`, `decide_response`) — I/O 없이 테스트 가능
- **selfcheck 테스트** (app.py:515-664) — 디바운스, 세션, 미반납, 재고 변경, DB 왕복 등 핵심 시나리오 커버
- **디바운스 설계** — 단발 오검출 방지, 재고 변경 시 재기준선 대기(confirmed=None) 등 엣지 케이스 대응
- **F6: 스트릭 시작 시점 세션 스냅샷** — 디바운스 지연 중 다른 사람 태깅 시 오귀속 방지
- **RFID UID 소비 전략** (client.py:52-61) — 전송 실패 시 UID 유지, 성공 시에만 소비
- **경광등 상태 캐싱** (hw.py:46-55) — 동일 상태 재적용 시 릴레이 클릭/blink 재시작 방지

---

## 📋 수정 우선순위 (권장)

> [!IMPORTANT]
> 아래 순서대로 수정하면 가장 큰 리스크부터 해소할 수 있다.

1. **C3 + C1**: 로깅 도입 + `except Exception: pass` 제거 — 디버깅 기반 확보
2. **C1**: YOLO 추론을 `_state_lock` 밖으로 — 대시보드 블로킹 해소
3. **C5**: DB 트랜잭션 일관성 — 부분 커밋 방지
4. **C2**: `/frame` API 인증 — 상태 조작 방어
5. **C4**: 비밀번호·secret_key 환경변수 이관
6. **W9**: 업로드 크기 제한 (`MAX_CONTENT_LENGTH`)
7. **W2, W4, W5**: 입력 검증 강화 (서버 응답, LED color, GPIO 핀)
