# PRD — Vendedor Virtual Inmobiliario (VVI)
### Product Requirements Document · MVP 72h

| Campo | Valor |
|---|---|
| Producto | Vendedor Virtual Inmobiliario (VVI) |
| Versión | 1.0 (MVP) |
| Mercado | Propiedades residenciales — Medellín y Pereira (Colombia) |
| Plazo | 72 horas (Fase 1) |
| Stack de construcción | Claude Code (build) + Kimi K2.6 / Claude (runtime LLM) |
| Modelo de negocio | Comisión del **3%** sobre venta confirmada |
| Estado legal base | Ley 1581/2012 + Decreto 1377/2013 (reforma en trámite 2025–2026) |

> **Aviso legal.** Este documento incorpora requisitos de cumplimiento pero **no constituye asesoría jurídica**. Antes de salir a producción, valida el texto vigente de la Ley 1581/2012 y su reforma con un abogado especializado en protección de datos en Colombia.

---

## 1. Resumen ejecutivo

VVI es un **agente conversacional de IA** que atiende, califica y empareja prospectos compradores de vivienda en Medellín y Pereira contra una cartera de propiedades, y entrega los prospectos calificados a un **agente humano** que cierra la venta. Está dirigido a inmobiliarios y propietarios que tienen inventario pero no un canal eficiente de atención y calificación. Existe porque hoy los leads inmobiliarios se pierden por respuesta lenta y mala calificación; el bot responde en segundos, filtra por intención real de compra y agenda al humano solo cuando hay oportunidad.

**Decisión de arquitectura clave (léase antes que nada):** el MVP **no** rastrea (scrapea) ni contacta en frío a personas en redes sociales. Eso viola la Ley 1581/2012 (exige autorización previa) y los Términos de Servicio de las plataformas. VVI opera bajo un modelo **inbound / opt-in**: el comprador inicia o autoriza el contacto. Ver ADR-01.

---

## 2. User Personas

### Persona 1 — Prospecto comprador ("Andrés / Laura")
- **Perfil:** 28–50 años, busca casa/apto/lote en Medellín o Pereira; explora en Marketplace, Mercado Libre, OLX e Instagram.
- **Contexto:** navega desde el celular, envía mensajes cortos, quiere respuesta inmediata y sin fricción.
- **Objetivo:** encontrar rápido propiedades que encajen en su presupuesto y zona.
- **Frustraciones:** que nadie responda, que le manden info irrelevante, sentir que lo están "vendiendo" agresivamente.
- **Éxito para él/ella:** en 3–5 mensajes ya tiene 2–3 opciones reales y una visita agendada.

### Persona 2 — Propietario / Inmobiliario vendedor ("Doña Marta / Agencia Pereira")
- **Perfil:** dueño de 1–20 propiedades, o agente inmobiliario con cartera.
- **Contexto:** tiene inventario en Excel/WhatsApp, pierde leads por no responder a tiempo.
- **Objetivo:** vender más rápido sin contratar más gente; pagar solo cuando se cierra (comisión 3%).
- **Frustraciones:** leads fríos que no compran, no saber qué prospecto va en serio, desconfianza en "automatizar" la venta.
- **Éxito:** recibe solo prospectos calificados y con datos, y controla el momento del cierre.

### Persona secundaria — Operador humano (agente de cierre)
Persona interna que recibe prospectos calificados, hace la visita, negocia y **confirma la venta** en el dashboard. Es quien dispara el cálculo de comisión. Es el "humano en el loop" (ver ADR-05).

---

## 3. User Stories

| ID | Historia | Prioridad |
|---|---|---|
| HU-01 | Como **comprador**, quiero escribirle al bot por Telegram y recibir respuesta inmediata, para no esperar a un humano. | Must |
| HU-02 | Como **comprador**, quiero saber desde el primer mensaje que hablo con una IA, para decidir con transparencia si continúo. | Must |
| HU-03 | Como **comprador**, quiero que me pregunten zona, tipo, presupuesto y plazo, para recibir solo opciones que me sirven. | Must |
| HU-04 | Como **comprador**, quiero ver 2–3 propiedades que encajan con mi búsqueda, para comparar rápido. | Must |
| HU-05 | Como **comprador**, quiero autorizar explícitamente que me contacten y guarden mis datos, para tener control sobre mi información. | Must |
| HU-06 | Como **comprador**, quiero pedir una visita o hablar con un asesor humano, para avanzar cuando algo me interesa. | Must |
| HU-07 | Como **inmobiliario**, quiero cargar mis propiedades (dirección, tipo, precio, descripción), para que el bot las use al emparejar. | Must |
| HU-08 | Como **inmobiliario**, quiero ver un tablero con los prospectos, su estado y su nivel de intención, para priorizar mi tiempo. | Must |
| HU-09 | Como **operador**, quiero marcar manualmente una venta como cerrada e indicar el precio, para que el sistema calcule la comisión del 3%. Se debe evitar que el vendedor que cierra la venta no lo notifique por el sistema | Must |
| HU-10 | Como **operador**, quiero ver el registro de consentimiento y auditoría de cada prospecto, para responder ante la SIC si me lo exigen. | Must |
| HU-11 | Como **inmobiliario**, quiero que un prospecto sin intención real sea descartado automáticamente, para no perder tiempo. | Nice-to-have |
| HU-12 | Como **comprador**, quiero ejercer mis derechos (consultar/borrar mis datos) escribiéndole al bot, para cumplir mi derecho de habeas data. | Nice-to-have |

---

## 4. Requerimientos funcionales

> Prioridad: **Must** = entra en las 72h · **Nice** = queda fuera del MVP (Fase 2).

### 4.1 Canal de entrada
| ID | Requerimiento | Prioridad |
|---|---|---|
| RF-01 | Recibir mensajes entrantes de prospectos vía **bot de Telegram** (el comprador inicia). | Must |
| RF-02 | Permitir **carga manual** de un lead inbound por el operador (p. ej. una consulta que llegó por Marketplace/OLX y que el comprador ya inició). | Must |
| RF-03 | Integrar Meta Lead Ads (Instagram/Facebook), Mercado Libre y WhatsApp Cloud API como canales de entrada. | Nice (Fase 2) |

### 4.2 Motor conversacional y clasificación
| ID | Requerimiento | Prioridad |
|---|---|---|
| RF-04 | En el **primer mensaje**, identificarse como IA/bot y solicitar consentimiento de tratamiento de datos con enlace a política de privacidad. | Must |
| RF-05 | Extraer de la conversación los campos: `ciudad`, `zona`, `tipo` (casa/apto/lote), `presupuesto`, `habitaciones`, `plazo_compra`. | Must |
| RF-06 | Calcular un **score de intención de compra** (0–100) y una etiqueta (`frío` / `tibio` / `caliente`). | Must |
| RF-07 | Mantener un tono comercial pero honesto; no inventar propiedades ni precios que no existan en la cartera. | Must |
| RF-08 | Responder en español colombiano, natural, en mensajes cortos. | Must |

### 4.3 Emparejamiento y cartera
| ID | Requerimiento | Prioridad |
|---|---|---|
| RF-09 | Emparejar el perfil del prospecto contra la cartera (filtro por ciudad, tipo, rango de precio, habitaciones) y devolver **top 3**. | Must |
| RF-10 | Gestionar la cartera de propiedades (crear/leer/actualizar/inactivar). En el MVP con **datos mockeados** en JSON/SQLite. | Must |
| RF-11 | Presentar cada match con foto (placeholder), precio, zona y una frase de venta generada. | Must |

### 4.4 Handoff, venta y comisión
| ID | Requerimiento | Prioridad |
|---|---|---|
| RF-12 | Permitir al prospecto **solicitar visita / hablar con asesor**; el sistema notifica al operador. | Must |
| RF-13 | Mantener una **máquina de estados** del prospecto: `nuevo → calificado → emparejado → contactado → visita → oferta → vendido / perdido`. | Must |
| RF-14 | Permitir al operador **confirmar una venta** ingresando precio final; el sistema **calcula comisión = 3% × precio** y la atribuye al prospecto/sesión de origen. | Must |
| RF-15 | Registrar la atribución: qué lead, qué canal y qué propiedad originaron la venta. | Must |

### 4.5 Cumplimiento y auditoría
| ID | Requerimiento | Prioridad |
|---|---|---|
| RF-16 | Guardar el **consentimiento** (timestamp, texto de la autorización, canal) por cada prospecto antes de tratar sus datos. | Must |
| RF-17 | Cifrar en reposo los **datos de contacto** (nombre, teléfono, usuario). | Must |
| RF-18 | Mantener un **log de auditoría** inmutable de acciones sobre datos personales. | Must |
| RF-19 | Bloquear cualquier contacto saliente a un prospecto que **no** haya consentido. | Must |
| RF-20 | Permitir a un titular **consultar y solicitar borrado** de sus datos (habeas data). | Nice (Fase 2) |

---

## 5. Requerimientos no funcionales

| ID | Categoría | Requerimiento | Meta MVP |
|---|---|---|---|
| RNF-01 | Latencia | Tiempo de primera respuesta del bot | < 5 s (p95) |
| RNF-02 | Latencia | Respuesta con matches tras tener los datos | < 8 s (p95) |
| RNF-03 | Disponibilidad | Uptime del bot durante horario comercial | ≥ 95% (best-effort en MVP) |
| RNF-04 | Seguridad | Datos de contacto cifrados en reposo; TLS en tránsito | Obligatorio |
| RNF-05 | Seguridad | Claves/API keys fuera del código (variables de entorno / secretos) | Obligatorio |
| RNF-06 | Privacidad | Retención de datos con propósito definido; borrado configurable | Documentado |
| RNF-07 | Costo | Costo de LLM por conversación | ≤ USD 0,03 estimado |
| RNF-08 | Mantenibilidad | Prompts, plantillas y reglas configurables sin recompilar | Deseable |
| RNF-09 | Trazabilidad | Toda decisión que afecte al titular queda logueada | Obligatorio |

---

## 6. Criterios de aceptación (definición de "listo en 3 días")

| Feature | "Listo" significa |
|---|---|
| Bot Telegram (RF-01/04) | Un comprador escribe al bot, recibe el aviso de IA + consentimiento y, tras aceptar, entra en conversación. Sin aceptación, no se guardan datos personales. |
| Calificación (RF-05/06) | Tras 3–6 mensajes, el sistema tiene ciudad, tipo, presupuesto y un score de intención guardado en la base. |
| Matching (RF-09/11) | Con los datos completos, el bot devuelve hasta 3 propiedades de la cartera mock que cumplen ciudad + tipo + rango de precio. |
| Handoff (RF-12) | El prospecto puede pedir visita; el operador ve la solicitud en el tablero con los datos y el score. |
| Cartera (RF-10) | Existen ≥ 15 propiedades mock (Medellín + Pereira) cargadas y consultables. |
| Comisión (RF-14) | El operador marca "vendido", ingresa el precio y el sistema muestra la comisión = 3% y la deja registrada con su atribución. |
| Cumplimiento (RF-16/17/18) | Existe registro de consentimiento por prospecto, los teléfonos están cifrados y hay log de auditoría consultable. |
| Dashboard (HU-08) | Tabla de prospectos con estado, score y acciones; funcional aunque el diseño sea mínimo. |

---

## 7. Roadmap MVP

### Fase 1 — MVP (72 h) — *in scope*
- Canal único: **Telegram** (+ carga manual de leads inbound).
- Consentimiento + aviso de IA + política de privacidad.
- Clasificación de intención (reglas + LLM).
- Cartera **mockeada** (JSON/SQLite) y matching por filtros.
- Dashboard mínimo (prospectos, estados, comisión).
- Cifrado de contactos + logs de auditoría.
- Confirmación de venta humana + cálculo de comisión 3%.

### Fase 2 — Post-MVP — *out of scope ahora*
- Canales: **WhatsApp Cloud API**, **Instagram/Facebook vía Meta Lead Ads** (opt-in), **Mercado Libre** (API de preguntas), **email**.
- Clasificación con ML afinado sobre datos propios.
- Landing page con formulario opt-in y remarketing.
- CRM integrado, agenda de visitas, portabilidad y borrado automatizado de datos (RF-20).
- Panel de métricas avanzado.

### Fuera de alcance permanente (por diseño legal)
- Scraping de datos personales en redes sociales.
- Contacto en frío a personas que no iniciaron ni autorizaron el contacto.

---

## 8. Dependencias técnicas

| Dependencia | Uso | Costo aprox. | Riesgo |
|---|---|---|---|
| **Telegram Bot API** | Canal de entrada MVP | Gratis | Bajo — token instantáneo, sin verificación |
| **Kimi API** (`api.moonshot.ai/v1`, `kimi-k2.6`) | Conversación + clasificación | ~USD 0,95/1M in · 4,00/1M out | Bajo — compatible OpenAI, requiere top-up mínimo |
| **Anthropic API** (Claude) | Alternativa/fallback del LLM y **Claude Code** para construir | Según uso | Bajo |
| **SQLite** | Almacenamiento local | Gratis | Bajo |
| Librería de cifrado (Fernet/`cryptography`) | Cifrado de PII | Gratis | Bajo |
| WhatsApp Cloud API | Canal Fase 2 | Verificación 3–10 días hábiles | **Alto para 72h** → por eso NO entra al MVP |
| Meta Graph / Lead Ads | Prospección opt-in Fase 2 | Costo de pauta | Medio |
| Mercado Libre API | Inbound Fase 2 | Gratis (con app registrada) | Medio |

---

## 9. Métricas de éxito

| Métrica | Definición | Meta piloto |
|---|---|---|
| Prospectos atendidos | Conversaciones inbound gestionadas por el bot | Baseline en semana 1 |
| Tiempo de primera respuesta | Segundos hasta la primera respuesta | < 5 s |
| Tasa de calificación | % de prospectos con datos completos + score | ≥ 60% |
| Tasa de match | % de prospectos calificados que reciben ≥1 propiedad | ≥ 80% |
| Visitas agendadas | Prospectos que piden visita/asesor | Contador |
| Ventas confirmadas | Cierres marcados por el operador | Contador |
| Comisión generada | Σ (3% × precio de venta) | Contador |
| Tasa de consentimiento | % de prospectos que aceptan tratamiento | Monitoreo |

---

## 10. Control de comisión (requisito de negocio)

- La comisión (**3%**) se genera **solo** cuando el operador humano marca la venta como confirmada (RF-14). Ninguna venta se cierra de forma automática. Debemos evitar que el humano venda y no lo reporte por el sistema.
- El sistema **atribuye** la venta al prospecto, canal y propiedad de origen para justificar la comisión ante el propietario.
- El registro de comisión incluye: `id_venta`, `id_prospecto`, `id_propiedad`, `precio_venta`, `comision = precio × 0.03`, `fecha`, `operador`.
- Este gate humano no solo es de negocio: también responde al derecho (en refuerzo con la reforma 2025–2026) a **no ser objeto de decisiones automatizadas** con efectos jurídicos/económicos. Ver ADR-05.
