# ADR — Vendedor Virtual Inmobiliario (VVI)
### Architecture Decision Record · MVP 72h

| Campo | Valor |
|---|---|
| Sistema | Vendedor Virtual Inmobiliario (VVI) |
| Versión | 1.0 (MVP) |
| Fecha | Agosto 2026 |
| Documentos relacionados | PRD, SRS |

> **Aviso legal.** Estas decisiones incorporan criterios de cumplimiento pero no sustituyen asesoría jurídica. Valida con abogado especializado en protección de datos en Colombia (Ley 1581/2012 y su reforma 2025–2026).

---

## ADR-01 — Estrategia de obtención de prospectos: inbound / opt-in, NO scraping

**Contexto.** El objetivo de negocio es "identificar y contactar prospectos en Instagram, Facebook, Marketplace, OLX y Mercado Libre". Sin embargo, tus propias restricciones prohíben el scraping no autorizado, y la Ley 1581/2012 exige **autorización previa, expresa e informada** del titular antes de tratar sus datos.

**Decisión.** El sistema se construye sobre un modelo **inbound / opt-in**: el prospecto **inicia** el contacto (bot de Telegram) o **autoriza** explícitamente ser contactado (Fase 2: formularios de Meta Lead Ads, preguntas en Mercado Libre, landing con consentimiento). **No** se rastrean ni se compran datos personales de redes sociales para contacto en frío.

**Justificación.**
- La Ley 1581/2012 y el Decreto 1377/2013 exigen consentimiento previo; el contacto en frío a datos raspados es sancionable (multas que con la reforma podrían llegar a 10.000 SMMLV o 5% de ingresos).
- Los Términos de Servicio de Meta, Mercado Libre y OLX prohíben scraping y automatización no autorizada; el riesgo es bloqueo de cuentas y responsabilidad legal.
- WhatsApp/Meta estructuralmente bloquean el saliente no solicitado (ventana de 24 h, plantillas pre-aprobadas, opt-in), así que la vía de "cold outreach" ni siquiera es técnicamente estable.

**Implicaciones.**
- ✅ Legal, sostenible y alineado con tus restricciones.
- ➖ El volumen de prospectos depende de generar inbound (pauta, listados, link-in-bio), no de "cosechar" contactos.
- ➖ Requiere invertir en Fase 2 (Lead Ads, listados) para escalar la entrada.

**Alternativas rechazadas.**
- *Scraping + contacto en frío:* rechazada por ilegal (Ley 1581), contraria a ToS y de alto riesgo de sanción y baneo.
- *Compra de bases de datos:* rechazada; el origen sin consentimiento es igualmente no conforme.

---

## ADR-02 — Canal de contacto del MVP: Telegram (WhatsApp en Fase 2)

**Contexto.** Se pidió incluir WhatsApp o Telegram, "lo más fácil que se pueda", en 72 h. En Colombia WhatsApp domina, pero su API tiene fricción de onboarding.

**Decisión.** Usar **Telegram Bot API** como único canal del MVP. WhatsApp Cloud API se pospone a Fase 2.

**Justificación.**
- Telegram entrega token de bot al instante (BotFather), sin verificación de negocio ni aprobación de plantillas; funciona con long-polling sin infraestructura.
- WhatsApp Cloud API requiere Meta Business Verification (típicamente 2–4 días) + aprobación de plantillas (24–48 h) + WABA; el rango real de onboarding es de 3–10 días hábiles, incompatible con la ventana de 72 h.

**Implicaciones.**
- ✅ Canal demostrable en horas, gratis.
- ➖ Telegram tiene menos penetración que WhatsApp en el público objetivo → el piloto valida el flujo, no el volumen final.
- ➕ El diseño abstrae el canal (`channel_gateway`), así que añadir WhatsApp en Fase 2 no reescribe la lógica.

**Alternativas rechazadas.**
- *WhatsApp Cloud API en el MVP:* rechazada por tiempos de verificación que consumen toda la ventana de 72 h.
- *Librerías no oficiales de WhatsApp:* rechazadas; violan ToS y provocan baneo del número.

---

## ADR-03 — Clasificación de prospectos: híbrido reglas + LLM (no ML entrenado)

**Contexto.** Hay que identificar intención de compra y extraer datos (ciudad, tipo, presupuesto) en 72 h, sin dataset propio.

**Decisión.** Usar un **híbrido**: reglas duras para filtros no negociables (ciudad ∈ {Medellín, Pereira}, presupuesto mínimo) + **LLM (Kimi K2.6)** para extracción de slots y `score_intencion`, con salida JSON estricta.

**Justificación.**
- No hay datos etiquetados para entrenar un modelo propio en 72 h.
- Un LLM zero/few-shot extrae entidades y evalúa intención con calidad suficiente para MVP y cero entrenamiento.
- Las reglas duras garantizan decisiones determinísticas y baratas donde no se necesita LLM (ahorra tokens y evita errores).

**Implicaciones.**
- ✅ Cero fase de entrenamiento; ajustable vía prompt.
- ➖ Costo por token y latencia por llamada al LLM (mitigado con reglas + caching de prefijo).
- ➖ Menos preciso que un modelo afinado con datos reales → se afina en Fase 2.

**Alternativas rechazadas.**
- *ML entrenado (clasificador supervisado):* rechazado; sin datos y sin tiempo en el MVP.
- *Solo reglas/keywords:* rechazado; frágil ante lenguaje natural y coloquial.

---

## ADR-04 — Almacenamiento: SQLite local + cifrado de campo (herramientas libres/sin costo)

**Contexto.** El MVP debe usar **almacenamiento libre o sin costo** en esta fase, y debe cumplir normativa de datos personales.

**Decisión.** **SQLite** local para persistencia + **cifrado de campo (Fernet)** para PII de contacto; secretos en variables de entorno.

**Justificación.**
- SQLite es gratuito, open-source, cero-config y suficiente para el volumen de un piloto.
- Mantener los datos localmente (en jurisdicción) simplifica el cumplimiento colombiano y evita transferencias internacionales no reguladas en el MVP.
- El cifrado de campo protege el dato más sensible (teléfono/usuario) incluso si el archivo se filtra.

**Implicaciones.**
- ✅ Sin costo de base de datos; cumplimiento de datos en reposo.
- ➖ SQLite no soporta alta concurrencia ni réplica → migrar a Postgres gestionado en Fase 2.
- ➖ La gestión de la clave de cifrado debe cuidarse (rotación, backup) desde ya.
- ➕ Debe evaluarse el registro ante el **RNBD** (Registro Nacional de Bases de Datos) según aplique.

**Alternativas rechazadas.**
- *Base de datos en la nube de pago:* rechazada por la restricción de costo del MVP.
- *Guardar PII en texto plano:* rechazado; incumple seguridad by design y la Ley 1581.

---

## ADR-05 — Humano en el loop para el cierre de venta (y la comisión)

**Contexto.** El negocio cobra 3% por venta confirmada y necesita controlar cuándo se realiza. Además, la reforma de la Ley 1581 refuerza el derecho a **no ser objeto de decisiones automatizadas** con efectos jurídicos/económicos.

**Decisión.** La IA **no** cierra ventas ni genera comisión de forma autónoma. Un **operador humano** confirma la venta e ingresa el precio; solo entonces el sistema calcula la comisión (3%) y la atribuye.

**Justificación.**
- Evita cobros indebidos y disputas sobre atribución de comisión.
- Cumple el principio (reforzado en la reforma) de intervención humana en decisiones con efectos económicos.
- Da a los propietarios control y confianza sobre el momento del cierre.

**Implicaciones.**
- ✅ Comisión trazable y defendible; cumplimiento reforzado.
- ➖ Introduce un paso manual (no 100% automático), aceptable y deseable en este dominio.

**Alternativas rechazadas.**
- *Cierre y comisión automáticos por la IA:* rechazado por riesgo legal, de negocio y reputacional.

---

## ADR-06 — Proveedor de LLM y tooling: Kimi K2.6 en runtime, Claude Code para construir

**Contexto.** Se pidió específicamente Claude Code y Kimi. Hay que elegir qué usa el sistema en tiempo de ejecución y qué se usa para construirlo, cuidando costo.

**Decisión.** **Claude Code** para **construir** el MVP (scaffolding, integración, dashboard). **Kimi K2.6** vía API compatible con OpenAI (`api.moonshot.ai/v1`) como **LLM de runtime** para conversación/clasificación, con **Claude (Anthropic API)** como fallback configurable.

**Justificación.**
- Claude Code acelera la construcción agéntica del sistema dentro de la ventana de 72 h.
- Kimi K2.6 es económico ($0,95 / $4,00 por 1M tokens aprox.), soporta JSON mode y tool calls, y su compatibilidad con el SDK de OpenAI hace trivial cambiar de proveedor.
- Tener Claude como fallback evita dependencia de un solo proveedor y cubre picos o degradaciones.

**Implicaciones.**
- ✅ Bajo costo por conversación; portabilidad entre proveedores por la interfaz OpenAI-compatible.
- ➖ Dependencia de disponibilidad/rate limits del proveedor → mitigado con fallback y colas.
- ➕ Nota: `kimi-k2.5` tiene retiro anunciado (31-ago-2026); por eso el runtime fija **K2.6** (o K2.7 Code para tareas de código).

**Alternativas rechazadas.**
- *Un solo proveedor sin fallback:* rechazado por riesgo de indisponibilidad.
- *Modelo self-hosted (pesos abiertos de Kimi):* rechazado en MVP por costo de infraestructura GPU; reconsiderar en Fase 2 si se requiere control total del dato.
