"""Human-readable Step-7 match remarks."""

from __future__ import annotations

def generate_matching_remarks(match_type, booking_code_matched=None, hbd_gst_matched=None,
                              client_gst_matched=None, guest_score=None, checkin_delta=None,
                              checkout_delta=None):
    """Generate human-readable remarks describing how an invoice was matched.

    Args:
        match_type: "booking_code" or "gst"
        booking_code_matched: True/False (only for booking_code type)
        hbd_gst_matched: True/False (only for gst type)
        client_gst_matched: True/False (only for gst type)
        guest_score: 0-100 (RapidFuzz score)
        checkin_delta: -1, 0, or 1 (date difference in days)
        checkout_delta: -1, 0, or 1 (date difference in days)

    Returns:
        str: Human-readable summary, or None if insufficient data
    """
    def _delta_text(d):
        if d is None:
            return None
        try:
            d = int(d)
            if d == -1:
                return "-1 day"
            if d == 0:
                return "same day"
            if d == 1:
                return "+1 day"
        except (ValueError, TypeError):
            pass
        return None

    try:
        parts = []
        if match_type == "booking_code":
            parts.append("Matched by booking code.")
            if booking_code_matched is not None:
                # 1 = match, 0 = no match for integer flags
                val = bool(booking_code_matched == 1) if isinstance(booking_code_matched, int) else bool(booking_code_matched)
                parts.append(f"Booking code matched: {'yes' if val else 'no'}.")
        elif match_type == "gst":
            parts.append("Matched by GST.")
            if hbd_gst_matched is not None:
                # 1 = match, 0 = no match for integer flags
                val = bool(hbd_gst_matched == 1) if isinstance(hbd_gst_matched, int) else bool(hbd_gst_matched)
                parts.append(f"HBD GST: {'yes' if val else 'no'}.")
            if client_gst_matched is not None:
                # 1 = match, 0 = no match for integer flags
                val = bool(client_gst_matched == 1) if isinstance(client_gst_matched, int) else bool(client_gst_matched)
                parts.append(f"Client GST: {'yes' if val else 'no'}.")

        if guest_score is not None:
            try:
                s = int(guest_score)
                if 0 <= s <= 100:
                    parts.append(f"Guest score: {s}.")
            except (ValueError, TypeError):
                pass

        ci = _delta_text(checkin_delta)
        co = _delta_text(checkout_delta)
        if ci is not None or co is not None:
            date_parts = []
            if ci is not None:
                date_parts.append(f"Check-in: {ci}")
            if co is not None:
                date_parts.append(f"Check-out: {co}")
            parts.append(" ".join(date_parts) + ".")

        if not parts:
            return None
        return " ".join(parts)
    except Exception as e:
        print(f"   ⚠️  Error generating remarks: {e}")
        return None
