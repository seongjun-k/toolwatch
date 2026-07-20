"""웹 푸시 발송 (E3). pywebpush로 VAPID 서명 후 브라우저 푸시 서버에 직접 전송한다.

발송 자체는 부가 기능이므로 실패해도 판정 흐름(대여/반납 확정)에 영향을 주면 안 된다
(확장계획.md §E3) — send_push는 예외를 절대 밖으로 던지지 않는다.
"""
import json
from datetime import datetime, timedelta

from pywebpush import WebPushException, webpush

import db


def send_push(conn, uid, title, body, config, vapid_key_path):
    """uid의 모든 구독에 발송. 만료 구독(410/404)은 DB에서 제거, 그 외 실패는 로그만."""
    subs = db.get_subscriptions(conn, uid)
    if not subs:
        return
    with open(vapid_key_path, encoding="utf-8") as f:
        private_key = f.read()
    claims = {"sub": "mailto:" + config["vapid_email"]}
    payload = json.dumps({"title": title, "body": body}, ensure_ascii=False)
    for sub in subs:
        try:
            webpush(
                subscription_info={
                    "endpoint": sub["endpoint"],
                    "keys": json.loads(sub["keys_json"]),
                },
                data=payload,
                vapid_private_key=private_key,
                vapid_claims=dict(claims),
            )
        except WebPushException as e:
            status = e.response.status_code if e.response is not None else None
            if status in (404, 410):
                db.remove_subscription(conn, sub["endpoint"])
            else:
                print(f"[push] 발송 실패(uid={uid}): {e}")
        except Exception as e:  # 그 외 실패도 부가 기능이므로 삼킨다
            print(f"[push] 발송 실패(uid={uid}): {e}")


def select_reminders(loans, sent, now, morning_time, overdue_grace_sec=1800):
    """순수 함수: 미반납 loans + 이미 발송된 (loan_id, kind) 집합 → 발송할 [(loan_id, uid, kind, message)].

    loans: {"id", "uid", "tool", "due_at"(문자열 또는 None)} 목록. due_at 없는 loan은 대상 아님.
    sent: 이미 발송된 (loan_id, kind) 튜플 집합 (db.has_notice 대응).
    kind:
      - morning: due_at 날짜가 오늘이고 현재 시각이 morning_time 이후, 1회
      - due: now >= due_at, 1회
      - overdue: due_at + overdue_grace_sec 경과해도 여전히 미반납, 1회
    """
    reminders = []
    morning_h, morning_m = (int(x) for x in morning_time.split(":"))
    today = now.date()

    for loan in loans:
        due_raw = loan.get("due_at")
        if not due_raw:
            continue
        loan_id = loan["id"]
        uid = loan["uid"]
        tool = loan.get("tool", "")
        due_at = datetime.strptime(due_raw, "%Y-%m-%d %H:%M:%S")

        if due_at.date() == today:
            morning_at = due_at.replace(hour=morning_h, minute=morning_m, second=0, microsecond=0)
            if now >= morning_at and (loan_id, "morning") not in sent:
                reminders.append((loan_id, uid, "morning", f"{tool} 오늘 반납 기한입니다"))

        if now >= due_at and (loan_id, "due") not in sent:
            reminders.append((loan_id, uid, "due", f"{tool} 반납 기한이 도래했습니다"))

        if now >= due_at + timedelta(seconds=overdue_grace_sec) and (loan_id, "overdue") not in sent:
            reminders.append((loan_id, uid, "overdue", f"{tool} 반납이 지연되고 있습니다"))

    return reminders


def _selfcheck():
    """morning/due/overdue 각 1건 + 중복 방지 + due_at 없는 loan 제외를 시각 주입으로 검증."""
    from datetime import datetime as dt

    morning_time = "09:00"

    # morning: due_at이 오늘 날짜, 현재 09:30 -> morning 대상
    loans = [{"id": 1, "uid": "U1", "tool": "니퍼", "due_at": "2026-07-17 18:00:00"}]
    now = dt(2026, 7, 17, 9, 30, 0)
    result = select_reminders(loans, sent=set(), now=now, morning_time=morning_time)
    assert result == [(1, "U1", "morning", "니퍼 오늘 반납 기한입니다")], result

    # 이미 morning 발송됨 -> 재선별 안 됨
    result2 = select_reminders(loans, sent={(1, "morning")}, now=now, morning_time=morning_time)
    assert result2 == [], result2

    # due: now가 due_at을 지남 (morning 이미 발송됨 가정)
    now_due = dt(2026, 7, 17, 18, 0, 0)
    result3 = select_reminders(loans, sent={(1, "morning")}, now=now_due, morning_time=morning_time)
    assert result3 == [(1, "U1", "due", "니퍼 반납 기한이 도래했습니다")], result3

    # overdue: due_at + grace(1800초) 경과, 여전히 미반납 (due는 이미 발송됨)
    now_overdue = dt(2026, 7, 17, 18, 31, 0)
    result4 = select_reminders(loans, sent={(1, "morning"), (1, "due")}, now=now_overdue, morning_time=morning_time)
    assert result4 == [(1, "U1", "overdue", "니퍼 반납이 지연되고 있습니다")], result4

    # 이미 overdue까지 발송됨 -> 더 이상 대상 아님
    result5 = select_reminders(
        loans, sent={(1, "morning"), (1, "due"), (1, "overdue")}, now=now_overdue, morning_time=morning_time
    )
    assert result5 == [], result5

    # due_at 없는 loan은 대상 제외
    loans_no_due = [{"id": 2, "uid": "U2", "tool": "펜치", "due_at": None}]
    result6 = select_reminders(loans_no_due, sent=set(), now=now_overdue, morning_time=morning_time)
    assert result6 == [], result6

    print("push selfcheck OK")


if __name__ == "__main__":
    _selfcheck()
