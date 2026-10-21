"""Demo mode: fictional users and meetings of "Acme Corp".

Enabled with DEMO_MODE=1. Everything here is invented; any resemblance to real
people or companies is coincidental. The data is seeded once, into an empty
DATA_DIR. When the speech-to-text service or the LLM cannot be reached, demo
mode falls back to the canned transcript and summaries below so the whole flow
can be tried on a laptop without models.
"""

import logging
import os
import re
import shutil
import uuid
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger("meetingnotes.demo")

DEMO_DOMAIN = "acme.example"
DEMO_USERS = [
    ("alice.moreno", True),
    ("ben.carter", False),
    ("priya.shah", False),
]
SAMPLE_AUDIO = Path(__file__).resolve().parent / "demo" / "sample-meeting.mp3"

SAMPLE_TRANSCRIPT = (
    "Good morning everyone, this is the weekly product sync for Acme Corp. Ben, can you start with the "
    "mobile release? Sure. Version 2.4 is feature complete. We found one crash on older Android phones, and "
    "the fix should land on Wednesday. If QA signs off on Thursday, we can ship on Friday. Great. Priya, how is "
    "the new onboarding flow doing? The first week looks good. Sign-up completion went from 62% to 71%, and "
    "support tickets about passwords dropped by about a third. Excellent. So, decisions: we ship version 2.4 "
    "on Friday if QA approves, and we roll out the new onboarding to all regions next week. Ben owns the "
    "release, Priya owns the rollout. Thanks everyone, see you next Monday."
)

SAMPLE_SUMMARY = {
    "en": """# Weekly product sync

## Mobile release 2.4
- **Ben** reported that version 2.4 is feature complete.
- One crash was found on older Android phones; the fix is expected on **Wednesday**.
- If QA signs off on **Thursday**, the release ships on **Friday**.

## New onboarding flow
- **Priya** shared first-week results: sign-up completion rose from **62% to 71%**.
- Password-related support tickets dropped by about a third.

## Decisions
| Decision | Owner | When |
|---|---|---|
| Ship 2.4 if QA approves | Ben | Friday |
| Roll out new onboarding to all regions | Priya | Next week |

## Next steps
- Land the Android crash fix (Ben, Wednesday).
- QA sign-off (Thursday).
- Next sync: Monday.
""",
    "es": """# Reunión semanal de producto

## Versión móvil 2.4
- **Ben** informa de que la versión 2.4 tiene todas las funcionalidades.
- Se detectó un fallo en móviles Android antiguos; la corrección llegará el **miércoles**.
- Si QA da el visto bueno el **jueves**, se publica el **viernes**.

## Nuevo flujo de alta
- **Priya** comparte los resultados de la primera semana: la finalización del registro pasó del **62% al 71%**.
- Las incidencias de soporte sobre contraseñas bajaron aproximadamente un tercio.

## Decisiones
| Decisión | Responsable | Cuándo |
|---|---|---|
| Publicar la 2.4 si QA la aprueba | Ben | Viernes |
| Desplegar el nuevo alta en todas las regiones | Priya | La semana que viene |

## Próximos pasos
- Corregir el fallo de Android (Ben, miércoles).
- Validación de QA (jueves).
- Próxima reunión: lunes.
""",
}

# (days ago, hour, title, description, tags, whisper model, transcript, summary_en, summary_es)
MEETINGS = [
    (0, 9, "Weekly product sync",
     "Weekly sync of the product team: mobile release and onboarding metrics.",
     ["product", "weekly"], "small", SAMPLE_TRANSCRIPT, SAMPLE_SUMMARY["en"], SAMPLE_SUMMARY["es"]),

    (0, 8, "Daily stand-up",
     "Ten-minute stand-up of the platform team.",
     ["standup", "platform"], "base",
     "Ben: Yesterday I finished the Android crash fix, today I'm pairing with QA on the release checklist. "
     "No blockers. Priya: I'm preparing the onboarding rollout plan for the other regions; I need the "
     "translated e-mail templates by Wednesday. Alice: I'll ask the localisation team today. Anything else? "
     "Ben: The staging schedule is live, so staging will be scaled down tonight.",
     """# Daily stand-up

- **Ben**: Android crash fix done; working with QA on the release checklist. No blockers.
- **Priya**: preparing the regional onboarding rollout; needs translated e-mail templates by **Wednesday**.
- **Alice**: will chase the localisation team today.
- Staging is now scaled down at night (new schedule is live).
""",
     """# Reunión diaria

- **Ben**: corrección del fallo de Android terminada; trabaja con QA en la lista de la versión. Sin bloqueos.
- **Priya**: prepara el despliegue regional del nuevo alta; necesita las plantillas de correo traducidas para el **miércoles**.
- **Alice**: hablará hoy con el equipo de localización.
- Staging ya se reduce por las noches (la programación está activa).
"""),

    (1, 12, "Support escalations triage",
     "Weekly review of escalated support tickets.",
     ["support", "weekly"], "small",
     "Priya: Six escalations this week, down from eleven. Four are about invoice exports timing out for "
     "large accounts. Ben: That is the export job running in a single request; we can move it to a background "
     "job and e-mail a link. Alice: Let's do that next sprint and send the affected customers a workaround "
     "today. The other two are feature requests, I'll add them to the backlog.",
     """# Support escalations triage

## Overview
- **6 escalations** this week (11 last week).

## Invoice export time-outs (4 tickets)
- Large accounts hit a time-out because the export runs in a single request.
- **Decision**: move exports to a background job that e-mails a download link (next sprint, Ben).
- Send affected customers a workaround today (Priya).

## Other
- Two feature requests added to the backlog (Alice).
""",
     """# Triaje de escaladas de soporte

## Resumen
- **6 escaladas** esta semana (11 la anterior).

## Exportación de facturas que caduca (4 incidencias)
- Las cuentas grandes superan el tiempo máximo porque la exportación se hace en una sola petición.
- **Decisión**: pasar la exportación a una tarea en segundo plano que envíe un enlace por correo (próximo sprint, Ben).
- Enviar hoy una alternativa a los clientes afectados (Priya).

## Otros
- Dos peticiones de funcionalidad añadidas al backlog (Alice).
"""),

    (4, 14, "Design review: mobile settings",
     "Review of the redesigned settings screens for the mobile app.",
     ["design", "product"], "small",
     "Alice: The new settings screen groups everything into four sections. Priya: Users in the test sessions "
     "found notifications much faster, from forty seconds to under ten. Ben: The dark mode toggle needs a "
     "system default option. Alice: Agreed, add a third state. We'll ship it with version 2.5.",
     """# Design review: mobile settings

- New layout groups settings into **four sections**.
- Usability tests: finding notification settings dropped from **~40 s to under 10 s** (Priya).
- Dark mode toggle needs a **"use system setting"** option (Ben); agreed as a third state.
- Target release: **2.5**.
""",
     """# Revisión de diseño: ajustes de la app móvil

- El nuevo diseño agrupa los ajustes en **cuatro secciones**.
- Pruebas con usuarios: encontrar las notificaciones pasó de **~40 s a menos de 10 s** (Priya).
- El interruptor de modo oscuro necesita la opción **"usar ajuste del sistema"** (Ben); se acepta como tercer estado.
- Versión objetivo: **2.5**.
"""),

    (1, 16, "Q4 roadmap planning",
     "Prioritising the Q4 roadmap with engineering and design leads.",
     ["roadmap", "planning"], "medium",
     "Alice: Let's go through the Q4 candidates. We have offline mode, the reporting dashboard and SSO for "
     "enterprise customers. Ben: Offline mode is the biggest effort, roughly eight weeks for two engineers. "
     "Priya: Customers ask for SSO in almost every enterprise deal, and it is about three weeks. Alice: Then SSO "
     "goes first. Ben, can you split offline mode into a smaller first milestone? Ben: Yes, read-only offline "
     "access is doable in four weeks. Alice: Good. Reporting dashboard moves to Q1. I'll share the plan with "
     "the leadership team on Friday.",
     """# Q4 roadmap planning

## Candidates discussed
- **Enterprise SSO**: requested in most enterprise deals, ~3 weeks.
- **Offline mode**: ~8 weeks for two engineers; Ben proposes a read-only first milestone (~4 weeks).
- **Reporting dashboard**: postponed.

## Decisions
1. SSO is the first Q4 item.
2. Offline mode starts with a read-only milestone.
3. The reporting dashboard moves to Q1.

## Action items
- **Ben**: scope the read-only offline milestone.
- **Alice**: present the plan to leadership on Friday.
""",
     """# Planificación del roadmap del T4

## Candidatos
- **SSO para empresas**: lo piden en casi todas las ventas a empresas, ~3 semanas.
- **Modo sin conexión**: ~8 semanas para dos ingenieros; Ben propone un primer hito de solo lectura (~4 semanas).
- **Panel de informes**: se aplaza.

## Decisiones
1. SSO es lo primero del T4.
2. El modo sin conexión empieza con un hito de solo lectura.
3. El panel de informes pasa al T1.

## Tareas
- **Ben**: acotar el hito sin conexión de solo lectura.
- **Alice**: presentar el plan a dirección el viernes.
"""),

    (3, 11, "Customer onboarding retrospective",
     "What went well and what did not in the September onboarding of Bluefin Logistics (fictional customer).",
     ["retro", "customers"], "base",
     "Priya: The data import took three days instead of one, mostly because the CSV templates were outdated. "
     "Ben: We also had two support escalations about user invitations landing in spam. Alice: What went well? "
     "Priya: The kickoff call and the training sessions got great feedback. Alice: Let's fix the templates this "
     "sprint and ask IT to review our e-mail sender settings. Ben will write a short import checklist.",
     """# Customer onboarding retrospective

## What went well
- Kickoff call and training sessions received very positive feedback.

## What did not
- Data import took **3 days instead of 1** because of outdated CSV templates.
- Two escalations: invitation e-mails landing in spam.

## Actions
- Update the CSV templates this sprint (**Priya**).
- Review e-mail sender settings with IT (**Alice**).
- Write an import checklist (**Ben**).
""",
     """# Retrospectiva del alta de cliente

## Lo que fue bien
- La reunión de arranque y las formaciones recibieron muy buenas valoraciones.

## Lo que no
- La importación de datos tardó **3 días en lugar de 1** por plantillas CSV desactualizadas.
- Dos escaladas: correos de invitación que llegaban a spam.

## Acciones
- Actualizar las plantillas CSV este sprint (**Priya**).
- Revisar la configuración del remitente con IT (**Alice**).
- Escribir una lista de comprobación para la importación (**Ben**).
"""),

    (9, 15, "Backend engineer interview debrief",
     "Panel debrief for a backend engineer candidate.",
     ["hiring"], "base",
     "Alice: Let's share scores. Ben: Strong system design, clear about trade-offs, a bit slow on the coding "
     "exercise. Priya: Very good communication, asked smart questions about our on-call process. Alice: Overall "
     "it sounds like a hire. Ben, can you run a short follow-up on testing practices? Then we decide by Thursday.",
     """# Backend engineer interview debrief

## Feedback
- **System design**: strong, clear about trade-offs (Ben).
- **Coding exercise**: correct but slow.
- **Communication**: very good; thoughtful questions about on-call (Priya).

## Outcome
- Leaning towards **hire**.

## Next steps
- Ben runs a short follow-up on testing practices.
- Final decision by **Thursday**.
""",
     """# Valoración de entrevista: ingeniero backend

## Valoraciones
- **Diseño de sistemas**: sólido y claro con los compromisos (Ben).
- **Ejercicio de código**: correcto pero lento.
- **Comunicación**: muy buena; preguntas acertadas sobre las guardias (Priya).

## Resultado
- Tendencia a **contratar**.

## Próximos pasos
- Ben hará una entrevista corta sobre prácticas de testing.
- Decisión final el **jueves**.
"""),

    (20, 10, "Infrastructure cost review",
     "Monthly review of cloud spend.",
     ["finance", "infra"], "small",
     "Ben: Cloud spend was 18% over budget last month. Most of it comes from the staging environment, which "
     "runs at full size all weekend. Alice: Can we scale staging down outside working hours? Ben: Yes, a "
     "schedule would save around 30% of staging cost. Priya: We should also delete the old analytics snapshots. "
     "Alice: Agreed. Ben sets up the schedule, Priya cleans the snapshots, and we review again next month.",
     """# Infrastructure cost review

## Findings
- Cloud spend **18% over budget**.
- Main driver: the staging environment runs at full size over weekends.

## Proposals
- Scale staging down outside working hours (~30% staging savings).
- Delete old analytics snapshots.

## Owners
- **Ben**: staging schedule.
- **Priya**: snapshot cleanup.
- Review again next month.
""",
     """# Revisión de costes de infraestructura

## Hallazgos
- El gasto en la nube supera el presupuesto en un **18%**.
- Causa principal: el entorno de staging funciona a tamaño completo los fines de semana.

## Propuestas
- Reducir staging fuera del horario laboral (~30% de ahorro en staging).
- Borrar las instantáneas antiguas de analítica.

## Responsables
- **Ben**: programación de staging.
- **Priya**: limpieza de instantáneas.
- Nueva revisión el mes que viene.
"""),

    (45, 12, "Kickoff: Project Falcon",
     "Kickoff of the new partner API project.",
     ["kickoff", "falcon"], "large-v3",
     "Alice: Welcome to the Project Falcon kickoff. The goal is a public partner API by March. Ben: We will "
     "start with authentication and rate limiting, then the orders endpoints. Priya: I will coordinate with "
     "the three pilot partners and collect their requirements in the next two weeks. Alice: We meet every "
     "second Tuesday. Risks so far: documentation effort and partner availability.",
     """# Kickoff: Project Falcon

## Goal
- Public **partner API** by **March**.

## Plan
1. Authentication and rate limiting (Ben).
2. Orders endpoints.
3. Requirements from three pilot partners within two weeks (Priya).

## Cadence and risks
- Meetings every second Tuesday.
- Risks: documentation effort, partner availability.
""",
     """# Arranque: Proyecto Falcon

## Objetivo
- **API pública para socios** en **marzo**.

## Plan
1. Autenticación y límites de uso (Ben).
2. Endpoints de pedidos.
3. Requisitos de los tres socios piloto en dos semanas (Priya).

## Cadencia y riesgos
- Reuniones cada dos martes.
- Riesgos: esfuerzo de documentación y disponibilidad de los socios.
"""),
]


def _paragraphs(transcript: str) -> str:
    """Put each speaker turn ("Name: ...") on its own paragraph."""
    return re.sub(r"\s*\b(Alice|Ben|Priya): ", r"\n\n**\1:** ", transcript).strip()


def seed(core):
    """Create the demo users and meetings if the database is empty.

    `core` is the app module (passed in to avoid a circular import).
    """
    if core.count_users() > 0:
        return
    password = os.getenv("DEMO_PASSWORD") or "demo"
    user_ids = {}
    for name, is_admin in DEMO_USERS:
        user_ids[name] = core.create_user(f"{name}@{DEMO_DOMAIN}", password, is_admin=is_admin)

    owner = user_ids["alice.moreno"]
    conn = core.get_db(owner)
    now = datetime.now()
    for i, (days, hour, title, desc, tags, model, transcript, sum_en, sum_es) in enumerate(MEETINGS):
        meeting_id = str(uuid.uuid4())
        date = (now - timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0)
        if days == 0 and date > now:
            # Keep "today" meetings in the past but still today.
            start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
            date = max(now - timedelta(minutes=25 + 40 * i), min(now, start_of_day + timedelta(minutes=i)))
        audio_path = None
        if i == 0 and SAMPLE_AUDIO.exists():  # the sample recording belongs to the first meeting
            folder = core.get_user_meetings_folder(owner) / meeting_id
            folder.mkdir(parents=True, exist_ok=True)
            audio_path = folder / SAMPLE_AUDIO.name
            shutil.copy(SAMPLE_AUDIO, audio_path)
        conn.execute(
            "INSERT INTO meetings (id, title, description, date, audio_path, transcription, model,"
            " transcription_language, summary_model, summary_en, summary_es, is_transcribing)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'en', ?, ?, ?, 0)",
            (meeting_id, title, desc, date.strftime("%Y-%m-%d %H:%M:%S"),
             str(audio_path) if audio_path else None, _paragraphs(transcript), model, core.SUMMARY_MODEL, sum_en, sum_es))
        conn.commit()
        core.set_meeting_tags(conn, meeting_id, tags)
    conn.close()
    logger.warning("DEMO MODE: seeded %d fictional users (password '%s') and %d meetings",
                   len(DEMO_USERS), password, len(MEETINGS))
