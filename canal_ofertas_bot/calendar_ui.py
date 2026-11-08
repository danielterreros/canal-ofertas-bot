from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta, timezone
import time
from zoneinfo import ZoneInfo

MADRID = ZoneInfo("Europe/Madrid")
MAX_SCHEDULE_DAYS = 30
MAX_SCHEDULE_SECONDS = MAX_SCHEDULE_DAYS * 86400
MONTHS = ("", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre")


def zone_label(zone: ZoneInfo) -> str:
    return zone.key.rsplit("/", 1)[-1].replace("_", " ")


def zone_description(zone: ZoneInfo) -> str:
    return zone_label(zone)


def check_picker_zone(state, zone: ZoneInfo):
    # Pickers created before this setting existed used Madrid.
    if state.get("zone", "Europe/Madrid") != zone.key:
        raise ValueError("La zona horaria ha cambiado. Abre de nuevo Programar desde la vista previa.")


def future_instant(local: datetime, now=None, zone: ZoneInfo = MADRID) -> float:
    candidates = set()
    for fold in (0, 1):
        utc = local.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
        if utc.astimezone(zone).replace(tzinfo=None) == local:
            candidates.add(utc.timestamp())
    if not candidates:
        raise ValueError(f"Esa hora no existe por el cambio de horario de {zone_label(zone)}. Elige otra.")
    if len(candidates) != 1:
        raise ValueError(f"Esa hora se repite por el cambio de horario de {zone_label(zone)} y es ambigua. Elige otra.")
    instant = candidates.pop()
    current = time.time() if now is None else now
    if instant <= current:
        raise ValueError(f"Elige una fecha y hora futuras en el horario de {zone_label(zone)}.")
    if instant > current + MAX_SCHEDULE_SECONDS:
        raise ValueError("Solo se puede programar durante los próximos 30 días.")
    return instant


def parse_manual(value: str, now=None, zone: ZoneInfo = MADRID) -> float:
    try:
        local = datetime.strptime(value, "%d/%m/%Y %H:%M")
    except ValueError:
        raise ValueError(f"Uso: /programar DD/MM/AAAA HH:MM ({zone.key}).") from None
    return future_instant(local, now, zone)


def when(instant: float, zone: ZoneInfo = MADRID) -> str:
    label = zone_label(zone)
    return datetime.fromtimestamp(instant, zone).strftime("%d/%m/%Y %H:%M") + f" ({label})"


def when_short(instant: float, zone: ZoneInfo = MADRID) -> str:
    return datetime.fromtimestamp(instant, zone).strftime("%d/%m/%Y %H:%M") + f" ({zone_label(zone)})"


def start(destination, now=None, zone: ZoneInfo = MADRID):
    today = datetime.fromtimestamp(time.time() if now is None else now, zone).date()
    return {"screen": "days", "month": today.strftime("%Y-%m"), "destination": destination, "zone": zone.key}


def candidate(state, hour: int, minute: int, now=None, zone: ZoneInfo = MADRID):
    check_picker_zone(state, zone)
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("Hora o minuto fuera de rango.")
    day = date.fromisoformat(state["day"])
    return future_instant(datetime(day.year, day.month, day.day, hour, minute), now, zone)


def hour_available(state, hour: int, now=None, zone: ZoneInfo = MADRID) -> bool:
    for minute in (0, 59):
        try:
            candidate(state, hour, minute, now, zone)
            return True
        except ValueError:
            pass
    return False


def transition(state, action, value, now=None, zone: ZoneInfo = MADRID):
    check_picker_zone(state, zone)
    state = dict(state)
    screen = state["screen"]
    current = time.time() if now is None else now
    today = datetime.fromtimestamp(current, zone).date()
    last_day = datetime.fromtimestamp(current + MAX_SCHEDULE_SECONDS, zone).date()
    if action == "month" and screen == "days":
        selected = datetime.strptime(value, "%Y-%m").date()
        if not today.replace(day=1) <= selected <= last_day.replace(day=1):
            raise ValueError("Elige un mes dentro de los próximos 30 días.")
        state["month"] = value
    elif action == "day" and screen == "days":
        chosen = date.fromisoformat(value)
        if not today <= chosen <= last_day:
            raise ValueError("Elige un día dentro de los próximos 30 días.")
        state.update(screen="hours", day=value)
        state.pop("hour", None)
        state.pop("due", None)
    elif action == "hour" and screen == "hours":
        hour = int(value)
        if not hour_available(state, hour, now, zone):
            raise ValueError("No quedan minutos válidos en esa hora. Elige otra.")
        state.update(screen="minutes", hour=hour)
        state.pop("due", None)
    elif action == "all" and screen == "minutes":
        state["screen"] = "all_minutes"
    elif action == "minute" and screen in ("minutes", "all_minutes"):
        state.update(screen="review", due=candidate(state, state["hour"], int(value), now, zone))
    elif action == "back":
        state["screen"] = {"hours": "days", "minutes": "hours", "all_minutes": "minutes", "review": "minutes"}.get(screen, "days")
        state.pop("due", None)
    elif action != "noop":
        raise ValueError("El control no corresponde a esta pantalla. Usa los botones actuales.")
    return state


def view(picker_token, state, now=None, zone: ZoneInfo = MADRID):
    check_picker_zone(state, zone)
    current = time.time() if now is None else now
    today = datetime.fromtimestamp(current, zone).date()
    last_day = datetime.fromtimestamp(current + MAX_SCHEDULE_SECONDS, zone).date()
    rows = []

    def button(label, action, value="-"):
        return {"text": label, "callback_data": f"s:{picker_token}:{action}:{value}"}

    screen = state["screen"]
    if screen == "days":
        cursor = datetime.strptime(state["month"], "%Y-%m").date()
        rows.append([button("Hoy", "day", today.isoformat()), button("Mañana", "day", (today + timedelta(days=1)).isoformat())])
        rows.append([button(f"{MONTHS[cursor.month]} {cursor.year}", "noop")])
        rows.append([button(label, "noop") for label in ("L", "M", "X", "J", "V", "S", "D")])
        for week in calendar.Calendar().monthdayscalendar(cursor.year, cursor.month):
            rows.append([button(str(number), "day", date(cursor.year, cursor.month, number).isoformat())
                         if number and today <= date(cursor.year, cursor.month, number) <= last_day
                         else button("·", "noop") for number in week])
        navigation = []
        if cursor > today.replace(day=1):
            navigation.append(button("◀ - Mes anterior", "month", (cursor - timedelta(days=1)).strftime("%Y-%m")))
        if cursor < last_day.replace(day=1):
            following = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
            navigation.append(button("Mes siguiente ▶", "month", following.strftime("%Y-%m")))
        if navigation:
            rows.append(navigation)
        text = (f"📅 Elige el día de publicación (próximos 30 días).\n"
                f"Horario de {zone_description(zone)}."
                + (" Todavía no se ha programado nada." if not state.get("reprogram") else ""))
    elif screen in ("hours", "minutes", "all_minutes"):
        choices = []
        numbers = range(24) if screen == "hours" else (range(60) if screen == "all_minutes" else range(0, 60, 5))
        for number in numbers:
            try:
                if screen == "hours":
                    if not hour_available(state, number, now, zone):
                        continue
                else:
                    candidate(state, state["hour"], number, now, zone)
            except ValueError:
                continue
            choices.append(button(f"{number:02d}", "hour" if screen == "hours" else "minute", str(number)))
        columns = 4 if screen == "minutes" else 6
        rows.extend(choices[index:index + columns] for index in range(0, len(choices), columns))
        if screen == "minutes":
            rows.append([button("Elegir cualquier minuto", "all")])
        selected = date.fromisoformat(state["day"]).strftime("%d/%m/%Y")
        text = f"🕒 Día {selected}. Elige la hora (00–23)." if screen == "hours" else f"🕒 {selected}, {state['hour']:02d}:__. Elige los minutos."
        text += f"\nHorario de {zone_description(zone)}."
        if not choices:
            text += "\nNo quedan instantes válidos. Vuelve atrás; las horas inexistentes o ambiguas por el cambio de horario no se admiten."
    elif screen == "review":
        text = (f"¿{'Reprogramar' if state.get('reprogram') else 'Programar'} para {when_short(state['due'], zone)}?\n"
                f"Destino: {state['destination']}")
        rows.append([button("✅ - Confirmar", "confirm")])
    else:
        raise ValueError("Pantalla del selector no válida.")
    rows.append([button("⬅️ - Volver", "back"), button("❌ - Cerrar", "close")])
    return text, rows
