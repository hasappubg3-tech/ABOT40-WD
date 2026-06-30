"""
غرف الدراسة الجماعية (Study Sessions)
======================================
special_action = "sessions"
callbacks prefix: ses_
states: wait_ses_study_time | wait_ses_break_time | wait_ses_password | wait_ses_join_pw
"""
import datetime
import logging
from .shared import *

logger = logging.getLogger(__name__)

ATTENDANCE_WINDOW = 120   # ثوانٍ لتسجيل الحضور بعد انتهاء الجلسة

# ══════════════════════════════════════════════════════════════════
# قاعدة البيانات
# ══════════════════════════════════════════════════════════════════

def _col_r():
    return get_mongo_db()["study_rooms"]

def _col_p():
    return get_mongo_db()["study_participants"]

def _strip(doc):
    if doc is None:
        return None
    d = dict(doc)
    d.pop("_id", None)
    return d

def _next_room_id():
    res = get_mongo_db()["_counters"].find_one_and_update(
        {"_id": "study_rooms"},
        {"$inc": {"seq": 1}},
        upsert=True, return_document=True,
    )
    return res["seq"]

# ── قراءة ──────────────────────────────────────────────────────────

def ses_get_room(rid: int):
    """يُرجع الغرفة النشطة فقط (غير المنتهية)."""
    return _strip(_col_r().find_one({"id": rid, "status": {"$ne": "ended"}}))

def _get_room_any(rid: int):
    """يُرجع الغرفة بأي حالة بما فيها المنتهية."""
    return _strip(_col_r().find_one({"id": rid}))

def ses_get_active_rooms():
    return [_strip(r) for r in _col_r().find(
        {"status": {"$in": ["waiting", "studying", "break", "attendance"]}},
        sort=[("created_at", 1)]
    )]

def ses_get_participants(rid: int):
    return [_strip(p) for p in _col_p().find({"room_id": rid}).sort("joined_at", 1)]

def _get_participant(rid: int, uid: int):
    return _strip(_col_p().find_one({"room_id": rid, "user_id": uid}))

def ses_is_in_room(rid: int, uid: int) -> bool:
    return bool(_col_p().count_documents({"room_id": rid, "user_id": uid}))

# ── إنشاء ──────────────────────────────────────────────────────────

def ses_create_room(creator_id, creator_name, study_time, break_time, password=None) -> int:
    rid = _next_room_id()
    _col_r().insert_one({
        "id": rid, "name": creator_name,
        "creator_id": creator_id, "creator_name": creator_name,
        "study_time": study_time, "break_time": break_time,
        "password": password, "status": "waiting",
        "created_at": datetime.datetime.utcnow(),
        "started_at": None, "current_session": 0,
        "current_phase_start": None, "last_phase_end": None,
        "attendance_open": False, "attendance_session": 0,
    })
    ses_join_room(rid, creator_id, creator_name)
    return rid

def ses_join_room(rid: int, uid: int, user_name: str) -> bool:
    if _col_p().count_documents({"room_id": rid, "user_id": uid}):
        return False
    room = ses_get_room(rid)
    now = datetime.datetime.utcnow()
    phase_join = now if room and room["status"] == "studying" else None
    _col_p().insert_one({
        "room_id": rid, "user_id": uid, "user_name": user_name,
        "joined_at": now, "phase_join_time": phase_join,
        "total_study_seconds": 0, "sessions_attended": 0,
        "last_confirmed_session": 0,
    })
    return True

def ses_leave_room(rid: int, uid: int):
    _col_p().delete_one({"room_id": rid, "user_id": uid})

# ── إدارة الجلسة ───────────────────────────────────────────────────

def ses_start_room(rid: int):
    """يبدأ الجلسة الأولى."""
    now = datetime.datetime.utcnow()
    _col_r().update_one({"id": rid}, {"$set": {
        "status": "studying", "started_at": now,
        "current_session": 1, "current_phase_start": now,
    }})
    _col_p().update_many({"room_id": rid}, {"$set": {"phase_join_time": now}})

def ses_open_attendance(rid: int, session_num: int, phase_end: datetime.datetime):
    """يفتح نافذة تسجيل الحضور."""
    _col_r().update_one({"id": rid}, {"$set": {
        "status": "attendance", "attendance_open": True,
        "attendance_session": session_num, "last_phase_end": phase_end,
    }})

def ses_start_break(rid: int):
    """يبدأ فترة الاستراحة."""
    now = datetime.datetime.utcnow()
    _col_r().update_one({"id": rid}, {"$set": {
        "status": "break", "current_phase_start": now, "attendance_open": False,
    }})
    _col_p().update_many({"room_id": rid}, {"$set": {"phase_join_time": None}})

def ses_next_study_phase(rid: int) -> int:
    """يبدأ جلسة دراسة جديدة ويُرجع رقمها."""
    now = datetime.datetime.utcnow()
    room = _get_room_any(rid)
    new_sn = (room.get("current_session") or 0) + 1
    _col_r().update_one({"id": rid}, {"$set": {
        "status": "studying", "current_session": new_sn,
        "current_phase_start": now, "attendance_open": False,
    }})
    _col_p().update_many({"room_id": rid}, {"$set": {"phase_join_time": now}})
    return new_sn

def ses_confirm_attendance(rid: int, uid: int, session_num: int):
    """يُسجّل الحضور ويحسب وقت الدراسة. يُرجع الثواني المضافة أو False."""
    p = _get_participant(rid, uid)
    if not p:
        return False
    if (p.get("last_confirmed_session") or 0) >= session_num:
        return False  # سبق التسجيل
    room = _get_room_any(rid)
    if not room:
        return False
    phase_start = room.get("current_phase_start") or datetime.datetime.utcnow()
    phase_end   = room.get("last_phase_end")       or datetime.datetime.utcnow()
    join_time   = p.get("phase_join_time")         or phase_start
    # وقت المشاركة الفعلي = من لحظة الانضمام للجلسة أو بدايتها أيهما أحدث
    contribution = max(0, (phase_end - max(join_time, phase_start)).total_seconds())
    _col_p().update_one({"room_id": rid, "user_id": uid}, {
        "$inc": {"total_study_seconds": int(contribution), "sessions_attended": 1},
        "$set": {"last_confirmed_session": session_num},
    })
    return int(contribution)

def ses_end_room(rid: int):
    _col_r().update_one({"id": rid}, {"$set": {
        "status": "ended", "ended_at": datetime.datetime.utcnow(),
    }})

# ── إحصائيات ──────────────────────────────────────────────────────

def ses_get_my_stats(uid: int) -> dict:
    pipeline = [
        {"$match": {"user_id": uid}},
        {"$group": {"_id": None,
            "total_secs":    {"$sum": "$total_study_seconds"},
            "total_sessions":{"$sum": "$sessions_attended"},
            "rooms":         {"$sum": 1},
        }}
    ]
    res = list(_col_p().aggregate(pipeline))
    return res[0] if res else {"total_secs": 0, "total_sessions": 0, "rooms": 0}

def ses_get_room_top(rid: int, limit: int = 10):
    return [_strip(p) for p in _col_p().find(
        {"room_id": rid},
    ).sort("total_study_seconds", -1).limit(limit)]

def ses_get_global_top(limit: int = 10):
    pipeline = [
        {"$group": {
            "_id": "$user_id",
            "user_name":      {"$first": "$user_name"},
            "total_secs":     {"$sum": "$total_study_seconds"},
            "total_sessions": {"$sum": "$sessions_attended"},
        }},
        {"$match": {"total_secs": {"$gt": 0}}},
        {"$sort": {"total_secs": -1}},
        {"$limit": limit},
    ]
    return list(_col_p().aggregate(pipeline))

# ══════════════════════════════════════════════════════════════════
# تنسيق النصوص
# ══════════════════════════════════════════════════════════════════

def _fmt_time(secs: int) -> str:
    secs = int(secs)
    if secs <= 0: return "0د"
    m = secs // 60
    if m < 60: return f"{m}د"
    h, m2 = divmod(m, 60)
    return f"{h}س {m2}د" if m2 else f"{h}س"

_STATUS_EMOJI = {
    "waiting":    "⏳ تنتظر البدء",
    "studying":   "📚 جلسة دراسة",
    "break":      "☕ استراحة",
    "attendance": "✋ تسجيل حضور",
    "ended":      "🏁 انتهت",
}

def ses_menu_text() -> str:
    rooms = ses_get_active_rooms()
    cnt = len(rooms)
    return (
        "🎓 *جلسات الدراسة الجماعية*\n\n"
        f"📡 الغرف المفتوحة حالياً: *{cnt}*\n\n"
        "انضم لغرفة موجودة أو أنشئ غرفتك الخاصة!"
    )

def _room_info_text(room, participants) -> str:
    lock   = "🔒" if room.get("password") else "🔓"
    status = _STATUS_EMOJI.get(room["status"], room["status"])
    sn     = room.get("current_session", 0)
    lines  = [
        f"🏠 *{room['name']}*",
        f"{lock} {'مقفلة' if room.get('password') else 'مفتوحة'} | {status}",
        "",
        f"📚 وقت الدراسة: *{room['study_time']} دقيقة*",
        f"☕ وقت الاستراحة: *{room['break_time']} دقيقة*",
        f"👥 المشاركون: *{len(participants)}*",
    ]
    if sn:
        lines.append(f"🔢 الجلسة الحالية: *{sn}*")
    return "\n".join(lines)

def ses_my_stats_text(uid: int) -> str:
    s = ses_get_my_stats(uid)
    return (
        "📊 *إحصائياتي*\n\n"
        f"⏱ إجمالي وقت الدراسة: *{_fmt_time(s.get('total_secs', 0))}*\n"
        f"✅ الجلسات المسجَّلة: *{s.get('total_sessions', 0)}*\n"
        f"🏠 الغرف المشارَك بها: *{s.get('rooms', 0)}*"
    )

def ses_room_stats_text(rid: int) -> str:
    room = _get_room_any(rid)
    if not room:
        return "❌ الغرفة غير موجودة."
    top    = ses_get_room_top(rid)
    medals = ["🥇", "🥈", "🥉"] + ["🏅"] * 7
    lines  = []
    for i, p in enumerate(top):
        medal = medals[i] if i < len(medals) else "🏅"
        secs  = p.get("total_study_seconds", 0)
        sess  = p.get("sessions_attended", 0)
        lines.append(f"{medal} {p['user_name']} — {_fmt_time(secs)} ({sess} جلسة)")
    body = "\n".join(lines) if lines else "_لا يوجد مشاركون بعد._"
    return (
        f"📊 *إحصائيات غرفة {room['name']}*\n"
        f"📚 {room['study_time']}د دراسة | ☕ {room['break_time']}د استراحة\n\n"
        + body
    )

def ses_global_stats_text() -> str:
    top    = ses_get_global_top()
    medals = ["🥇", "🥈", "🥉"] + ["🏅"] * 7
    lines  = []
    for i, p in enumerate(top):
        medal = medals[i] if i < len(medals) else "🏅"
        lines.append(
            f"{medal} {p['user_name']} — {_fmt_time(p.get('total_secs', 0))} "
            f"({p.get('total_sessions', 0)} جلسة)"
        )
    body = "\n".join(lines) if lines else "_لا توجد إحصائيات بعد._"
    return "🌍 *أفضل المستخدمين (كل الأوقات)*\n\n" + body

# ══════════════════════════════════════════════════════════════════
# لوحات المفاتيح
# ══════════════════════════════════════════════════════════════════

def kb_ses_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏠 عرض الغرف المتاحة", callback_data="ses_rooms")],
        [InlineKeyboardButton("➕ إنشاء غرفة جديدة",  callback_data="ses_create")],
        [
            InlineKeyboardButton("📊 إحصائياتي",      callback_data="ses_my_stats"),
            InlineKeyboardButton("🌍 الإحصائيات العامة", callback_data="ses_global_stats"),
        ],
    ])

def kb_ses_rooms(rooms) -> InlineKeyboardMarkup:
    rows = []
    st_emoji = {"waiting": "⏳", "studying": "📚", "break": "☕", "attendance": "✋"}
    for r in rooms:
        lock = "🔒" if r.get("password") else "🔓"
        st   = st_emoji.get(r["status"], "")
        cnt  = _col_p().count_documents({"room_id": r["id"]})
        label = f"{lock}{st} {r['name']} | {r['study_time']}/{r['break_time']}د | {cnt}👥"
        rows.append([InlineKeyboardButton(label, callback_data=f"ses_room_{r['id']}")])
    rows.append([
        InlineKeyboardButton("🔄 تحديث", callback_data="ses_rooms"),
        InlineKeyboardButton("🔙 رجوع",  callback_data="ses_menu"),
    ])
    return InlineKeyboardMarkup(rows)

def kb_ses_study_time() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("25 دقيقة", callback_data="ses_ct_25"),
            InlineKeyboardButton("45 دقيقة", callback_data="ses_ct_45"),
        ],
        [
            InlineKeyboardButton("60 دقيقة", callback_data="ses_ct_60"),
            InlineKeyboardButton("90 دقيقة", callback_data="ses_ct_90"),
        ],
        [InlineKeyboardButton("✏️ وقت مخصص", callback_data="ses_ct_c")],
        [InlineKeyboardButton("🔙 إلغاء",    callback_data="ses_menu")],
    ])

def kb_ses_break_time() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("5 دقائق",  callback_data="ses_cb_5"),
            InlineKeyboardButton("10 دقائق", callback_data="ses_cb_10"),
        ],
        [
            InlineKeyboardButton("15 دقيقة", callback_data="ses_cb_15"),
            InlineKeyboardButton("20 دقيقة", callback_data="ses_cb_20"),
        ],
        [InlineKeyboardButton("✏️ وقت مخصص", callback_data="ses_cb_c")],
        [InlineKeyboardButton("🔙 إلغاء",    callback_data="ses_menu")],
    ])

def kb_ses_privacy() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔓 عامة (بدون رمز)", callback_data="ses_priv_n"),
            InlineKeyboardButton("🔒 خاصة (برمز سري)", callback_data="ses_priv_y"),
        ],
        [InlineKeyboardButton("🔙 إلغاء", callback_data="ses_menu")],
    ])

def kb_ses_room(room, uid: int, is_in: bool) -> InlineKeyboardMarkup:
    rid    = room["id"]
    is_cr  = room["creator_id"] == uid
    status = room["status"]
    rows   = []
    if is_cr:
        if status == "waiting":
            rows.append([InlineKeyboardButton("🚀 بدء الجلسة", callback_data=f"ses_start_{rid}")])
        rows.append([InlineKeyboardButton("⏹ إنهاء الغرفة", callback_data=f"ses_end_{rid}")])
    elif not is_in:
        rows.append([InlineKeyboardButton("✅ انضمام للغرفة", callback_data=f"ses_join_{rid}")])
    else:
        rows.append([InlineKeyboardButton("🚪 مغادرة الغرفة", callback_data=f"ses_leave_{rid}")])
    rows.append([InlineKeyboardButton("📊 إحصائيات الغرفة", callback_data=f"ses_room_stats_{rid}")])
    rows.append([InlineKeyboardButton("🔙 الغرف المتاحة",   callback_data="ses_rooms")])
    return InlineKeyboardMarkup(rows)

def kb_ses_attendance(rid: int, sn: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✋ أنا موجود!", callback_data=f"ses_present_{rid}_{sn}")
    ]])

# ══════════════════════════════════════════════════════════════════
# مهام المؤقت (Job Queue)
# ══════════════════════════════════════════════════════════════════

async def _ses_study_end_job(ctx):
    """يُرسَل عند انتهاء وقت الدراسة."""
    data = ctx.job.data
    rid, sn = data["rid"], data["sn"]
    room = _get_room_any(rid)
    if not room or room["status"] != "studying" or room.get("current_session") != sn:
        return

    phase_end = datetime.datetime.utcnow()
    ses_open_attendance(rid, sn, phase_end)

    participants = ses_get_participants(rid)
    markup = kb_ses_attendance(rid, sn)
    for p in participants:
        try:
            await ctx.bot.send_message(
                chat_id=p["user_id"],
                text=(
                    f"⏰ *انتهت جلسة الدراسة!*\n\n"
                    f"🏠 الغرفة: *{room['name']}*\n"
                    f"📚 الجلسة رقم: *{sn}*\n\n"
                    f"⚡ اضغط *أنا موجود* خلال دقيقتين لتسجيل حضورك!"
                ),
                parse_mode="Markdown",
                reply_markup=markup,
            )
        except Exception:
            pass

    ctx.job_queue.run_once(
        _ses_attend_close_job,
        when=ATTENDANCE_WINDOW,
        data={"rid": rid, "sn": sn},
        name=f"ses_attend_{rid}_{sn}",
    )

async def _ses_attend_close_job(ctx):
    """يُغلق نافذة الحضور ويبدأ الاستراحة."""
    data = ctx.job.data
    rid, sn = data["rid"], data["sn"]
    room = _get_room_any(rid)
    if not room or room["status"] not in ("attendance", "studying"):
        return

    ses_start_break(rid)
    room = _get_room_any(rid)
    brk  = room["break_time"]

    participants = ses_get_participants(rid)
    for p in participants:
        try:
            await ctx.bot.send_message(
                chat_id=p["user_id"],
                text=(
                    f"☕ *وقت الاستراحة!*\n\n"
                    f"🏠 {room['name']} | ⏱ {brk} دقيقة\n\n"
                    "استرح قليلاً، ستبدأ الجلسة التالية قريباً 💤"
                ),
                parse_mode="Markdown",
            )
        except Exception:
            pass

    ctx.job_queue.run_once(
        _ses_break_end_job,
        when=datetime.timedelta(minutes=brk),
        data={"rid": rid},
        name=f"ses_break_{rid}_{sn}",
    )

async def _ses_break_end_job(ctx):
    """يبدأ جلسة الدراسة التالية بعد انتهاء الاستراحة."""
    data = ctx.job.data
    rid  = data["rid"]
    room = _get_room_any(rid)
    if not room or room["status"] != "break":
        return

    new_sn = ses_next_study_phase(rid)
    room   = _get_room_any(rid)
    study  = room["study_time"]

    participants = ses_get_participants(rid)
    for p in participants:
        try:
            await ctx.bot.send_message(
                chat_id=p["user_id"],
                text=(
                    f"📚 *ابدأ الدراسة الآن!*\n\n"
                    f"🏠 {room['name']} | الجلسة *{new_sn}*\n"
                    f"⏱ مدة الدراسة: *{study} دقيقة*\n\n"
                    "ركّز وابدأ! 💪"
                ),
                parse_mode="Markdown",
            )
        except Exception:
            pass

    ctx.job_queue.run_once(
        _ses_study_end_job,
        when=datetime.timedelta(minutes=study),
        data={"rid": rid, "sn": new_sn},
        name=f"ses_study_{rid}_{new_sn}",
    )

def _cancel_room_jobs(jq, rid: int, sn: int):
    """يلغي كل المهام المجدولة لغرفة معينة."""
    for name in [
        f"ses_study_{rid}_{sn}",
        f"ses_attend_{rid}_{sn}",
        f"ses_break_{rid}_{sn}",
        f"ses_break_{rid}_{sn - 1}",
    ]:
        for job in jq.get_jobs_by_name(name):
            job.schedule_removal()

# ══════════════════════════════════════════════════════════════════
# معالج الـ Callbacks
# ══════════════════════════════════════════════════════════════════

async def handle_ses_callback(q, ctx, uid: int, chat_id: int):
    d         = q.data
    user      = q.from_user
    user_name = user.first_name or user.username or str(uid)

    # ── القائمة الرئيسية ──────────────────────────────────────────
    if d == "ses_menu":
        await q.edit_message_text(
            ses_menu_text(), parse_mode="Markdown",
            reply_markup=kb_ses_main()
        )
        return

    # ── قائمة الغرف ───────────────────────────────────────────────
    if d == "ses_rooms":
        rooms = ses_get_active_rooms()
        if rooms:
            text = "🏠 *الغرف المتاحة الآن:*\n\naضغط على أي غرفة لعرض تفاصيلها."
        else:
            text = "🏠 *لا توجد غرف مفتوحة الآن.*\n\nكن الأول وأنشئ غرفتك!"
        await q.edit_message_text(
            text, parse_mode="Markdown",
            reply_markup=kb_ses_rooms(rooms)
        )
        return

    # ── إنشاء غرفة ────────────────────────────────────────────────
    if d == "ses_create":
        existing = list(_col_r().find({
            "creator_id": uid,
            "status": {"$in": ["waiting", "studying", "break", "attendance"]},
        }))
        if existing:
            await q.answer("⚠️ لديك غرفة مفتوحة بالفعل! أنهها أولاً.", show_alert=True)
            return
        await q.edit_message_text(
            "🏗 *إنشاء غرفة جديدة*\n\n"
            "ستُنشأ الغرفة باسمك تلقائياً.\n\n"
            "📚 اختر وقت الدراسة:",
            parse_mode="Markdown",
            reply_markup=kb_ses_study_time()
        )
        return

    # ── اختيار وقت الدراسة ────────────────────────────────────────
    if d.startswith("ses_ct_"):
        val = d[7:]
        if val == "c":
            ctx.user_data["state"] = "wait_ses_study_time"
            await q.edit_message_text(
                "⏱ أرسل وقت الدراسة بالدقائق (بين 5 و 180):",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ إلغاء", callback_data="ses_menu"),
                ]]),
            )
            return
        ctx.user_data["ses_study_time"] = int(val)
        await q.edit_message_text(
            f"✅ وقت الدراسة: *{val} دقيقة*\n\n☕ اختر وقت الاستراحة:",
            parse_mode="Markdown",
            reply_markup=kb_ses_break_time()
        )
        return

    # ── اختيار وقت الاستراحة ──────────────────────────────────────
    if d.startswith("ses_cb_"):
        val = d[7:]
        if val == "c":
            ctx.user_data["state"] = "wait_ses_break_time"
            await q.edit_message_text(
                "☕ أرسل وقت الاستراحة بالدقائق (بين 1 و 60):",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ إلغاء", callback_data="ses_menu"),
                ]]),
            )
            return
        ctx.user_data["ses_break_time"] = int(val)
        await q.edit_message_text(
            f"✅ وقت الاستراحة: *{val} دقيقة*\n\nهل الغرفة عامة أم خاصة؟",
            parse_mode="Markdown",
            reply_markup=kb_ses_privacy()
        )
        return

    # ── الخصوصية ──────────────────────────────────────────────────
    if d == "ses_priv_n":
        study = ctx.user_data.pop("ses_study_time", None)
        brk   = ctx.user_data.pop("ses_break_time", None)
        if not study or not brk:
            await q.answer("⚠️ انتهت الجلسة. ابدأ من جديد.", show_alert=True); return
        rid  = ses_create_room(uid, user_name, study, brk, password=None)
        room = ses_get_room(rid)
        pts  = ses_get_participants(rid)
        await q.edit_message_text(
            "✅ *تم إنشاء الغرفة!*\n\n" + _room_info_text(room, pts) +
            "\n\n🚀 اضغط *بدء الجلسة* عندما يكون الجميع جاهزاً.",
            parse_mode="Markdown",
            reply_markup=kb_ses_room(room, uid, True)
        )
        return

    if d == "ses_priv_y":
        if not ctx.user_data.get("ses_study_time") or not ctx.user_data.get("ses_break_time"):
            await q.answer("⚠️ انتهت الجلسة. ابدأ من جديد.", show_alert=True); return
        ctx.user_data["state"] = "wait_ses_password"
        await q.edit_message_text(
            "🔒 أرسل الرمز السري للغرفة:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ إلغاء", callback_data="ses_menu"),
            ]]),
        )
        return

    # ── عرض تفاصيل غرفة (يجب قبل ses_room_stats) ─────────────────
    if d.startswith("ses_room_") and not d.startswith("ses_room_stats_"):
        rid  = int(d[9:])
        room = ses_get_room(rid)
        if not room:
            await q.answer("⚠️ الغرفة غير موجودة أو انتهت.", show_alert=True); return
        pts  = ses_get_participants(rid)
        is_in = ses_is_in_room(rid, uid)
        await q.edit_message_text(
            _room_info_text(room, pts), parse_mode="Markdown",
            reply_markup=kb_ses_room(room, uid, is_in)
        )
        return

    # ── الانضمام لغرفة ────────────────────────────────────────────
    if d.startswith("ses_join_"):
        rid  = int(d[9:])
        room = ses_get_room(rid)
        if not room:
            await q.answer("⚠️ الغرفة غير موجودة أو انتهت.", show_alert=True); return
        if ses_is_in_room(rid, uid):
            await q.answer("أنت بالفعل في هذه الغرفة!", show_alert=False); return
        if room.get("password"):
            ctx.user_data["state"]       = "wait_ses_join_pw"
            ctx.user_data["ses_join_rid"] = rid
            await q.edit_message_text(
                f"🔒 الغرفة *{room['name']}* مقفلة\n\nأرسل الرمز السري للانضمام:",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ إلغاء", callback_data="ses_rooms"),
                ]]),
            )
            return
        ses_join_room(rid, uid, user_name)
        room = ses_get_room(rid)
        pts  = ses_get_participants(rid)
        await q.edit_message_text(
            "✅ *انضممت للغرفة!*\n\n" + _room_info_text(room, pts),
            parse_mode="Markdown",
            reply_markup=kb_ses_room(room, uid, True)
        )
        return

    # ── مغادرة الغرفة ────────────────────────────────────────────
    if d.startswith("ses_leave_"):
        rid  = int(d[10:])
        room = _get_room_any(rid)
        if room and room["creator_id"] == uid:
            await q.answer("❌ المنشئ لا يمكنه المغادرة. أنهِ الغرفة.", show_alert=True); return
        ses_leave_room(rid, uid)
        rooms = ses_get_active_rooms()
        await q.edit_message_text(
            "✅ *غادرت الغرفة.*",
            parse_mode="Markdown",
            reply_markup=kb_ses_rooms(rooms)
        )
        return

    # ── بدء الجلسة ───────────────────────────────────────────────
    if d.startswith("ses_start_"):
        rid  = int(d[10:])
        room = ses_get_room(rid)
        if not room:
            await q.answer("⚠️ الغرفة غير موجودة.", show_alert=True); return
        if room["creator_id"] != uid:
            await q.answer("❌ فقط منشئ الغرفة يمكنه البدء.", show_alert=True); return
        if room["status"] != "waiting":
            await q.answer("⚠️ الجلسة بدأت بالفعل!", show_alert=True); return

        ses_start_room(rid)
        study = room["study_time"]
        pts   = ses_get_participants(rid)

        for p in pts:
            if p["user_id"] != uid:
                try:
                    await ctx.bot.send_message(
                        chat_id=p["user_id"],
                        text=(
                            f"🚀 *بدأت الجلسة!*\n\n"
                            f"🏠 {room['name']} | الجلسة 1\n"
                            f"⏱ مدة الدراسة: *{study} دقيقة*\n\n"
                            "ركّز وابدأ! 💪"
                        ),
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass

        ctx.job_queue.run_once(
            _ses_study_end_job,
            when=datetime.timedelta(minutes=study),
            data={"rid": rid, "sn": 1},
            name=f"ses_study_{rid}_1",
        )

        room = ses_get_room(rid)
        pts  = ses_get_participants(rid)
        await q.edit_message_text(
            "🚀 *بدأت الجلسة!*\n\n" + _room_info_text(room, pts),
            parse_mode="Markdown",
            reply_markup=kb_ses_room(room, uid, True)
        )
        return

    # ── إنهاء الغرفة ─────────────────────────────────────────────
    if d.startswith("ses_end_"):
        rid  = int(d[8:])
        room = _get_room_any(rid)
        if not room:
            await q.answer("⚠️ الغرفة غير موجودة.", show_alert=True); return
        if room["creator_id"] != uid:
            await q.answer("❌ فقط منشئ الغرفة يمكنه الإنهاء.", show_alert=True); return

        sn = room.get("current_session", 1) or 1
        _cancel_room_jobs(ctx.job_queue, rid, sn)

        pts = ses_get_participants(rid)
        ses_end_room(rid)

        for p in pts:
            if p["user_id"] != uid:
                try:
                    await ctx.bot.send_message(
                        chat_id=p["user_id"],
                        text=(
                            f"🏁 *انتهت الغرفة*\n\n"
                            f"🏠 {room['name']}\n\n"
                            "شكراً لمشاركتك! 🎓"
                        ),
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass

        await q.edit_message_text(
            "✅ *تم إنهاء الغرفة.*\n\nيمكنك عرض الإحصائيات النهائية:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📊 إحصائيات الغرفة", callback_data=f"ses_room_stats_{rid}")],
                [InlineKeyboardButton("🔙 رجوع",            callback_data="ses_menu")],
            ])
        )
        return

    # ── تسجيل الحضور ─────────────────────────────────────────────
    if d.startswith("ses_present_"):
        rest = d[12:]               # "123_5"
        parts_sp = rest.rsplit("_", 1)
        rid, sn  = int(parts_sp[0]), int(parts_sp[1])
        room = _get_room_any(rid)
        if not room:
            await q.answer("⚠️ الغرفة انتهت.", show_alert=True); return
        result = ses_confirm_attendance(rid, uid, sn)
        if result is False:
            await q.answer("✅ تم تسجيل حضورك مسبقاً!", show_alert=False); return
        time_str = _fmt_time(result)
        await q.answer(f"✅ تم تسجيل حضورك! ({time_str} دراسة)", show_alert=True)
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    # ── إحصائياتي ────────────────────────────────────────────────
    if d == "ses_my_stats":
        await q.edit_message_text(
            ses_my_stats_text(uid), parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 رجوع", callback_data="ses_menu"),
            ]])
        )
        return

    # ── إحصائيات غرفة ────────────────────────────────────────────
    if d.startswith("ses_room_stats_"):
        rid = int(d[15:])
        room = _get_room_any(rid)
        back_cb = f"ses_room_{rid}" if ses_get_room(rid) else "ses_menu"
        await q.edit_message_text(
            ses_room_stats_text(rid), parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 رجوع", callback_data=back_cb),
            ]])
        )
        return

    # ── الإحصائيات العامة ─────────────────────────────────────────
    if d == "ses_global_stats":
        await q.edit_message_text(
            ses_global_stats_text(), parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 رجوع", callback_data="ses_menu"),
            ]])
        )
        return

    await q.answer()
