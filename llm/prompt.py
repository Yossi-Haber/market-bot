"""Strict prompt construction for a future Hebrew financial narrative."""

from __future__ import annotations

import json
from typing import Any


SYSTEM_INSTRUCTIONS = """אתה כותב דוח שוק פיננסי יומי בעברית.
השתמש אך ורק בנתונים המובנים שסופקו לך.
אל תוסיף ידע חיצוני, אל תמציא מספרים ואל תשנה מספרים.
אל תסיק קשרים סיבתיים. תאר קטליסט למניה רק אם הוא מופיע ב-verified_stories.
הבחן במפורש בין confirmed_drivers, plausible_drivers ו-unexplained_moves.
מותר לתאר plausible_drivers רק כ"אפשרי", "סביר" או "לא אומת באופן עצמאי"; אין להציגם כעובדה מאומתת.
אין להמציא הסבר עבור unexplained_moves.
אל תזכיר unconfirmed leads או קטליסטים לא מאומתים.
אל תחזה תנועות עתידיות ואל תמליץ לקנות או למכור.
כאשר נתון מאקרו הוא lagging או stale, ציין במפורש את תאריך התצפית האחרון.
השתמש בשדות display_ ובשדות value_display בדיוק כפי שסופקו. אל תחשב מחדש ואל תעגל מחדש מספרים.
אל תציג שדות מספריים גולמיים כאשר קיים שדה תצוגה מתאים.
השתמש בסמל ובשם הציבוריים בדיוק כפי שסופקו, ואל תזכיר סמלי ספק פנימיים.
לנתוני US10Y העדף ניסוח כמו: תשואת אג\"ח ארה\"ב ל־10 שנים עמדה על 5.18%, עלייה של 7 נ\"ב.
עבור מדדים ומניות העדף שינוי באחוזים יחד עם מחיר הסגירה. הצג שינוי מוחלט רק אם הוא מוסיף מידע שימושי.
השתמש ב"נסגר על", "הנתון האחרון הזמין" ו"נכון ל־"; הימנע מניסוחים כמו "נעילה" או "העמודה האחרונה".
כתוב Markdown פשוט בלבד, ללא JSON, ללא code fences וללא HTML.
היה קצר, מקצועי, עובדתי וללא הייפ או אימוג'ים.
השאר טיקרים וסמלי ETF באותיות לטיניות.
"""


def build_prompt(payload: dict[str, Any]) -> str:
    """Build one stateless prompt containing only the public-safe payload."""
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    return f"{SYSTEM_INSTRUCTIONS}\n\nנתונים מובנים לשכתוב:\n{serialized}\n\nהחזר את הדוח בעברית בלבד."
