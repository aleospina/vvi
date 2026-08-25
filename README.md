# VVI — Vendedor Virtual Inmobiliario

Agente conversacional de IA que **atiende, califica y empareja** compradores de vivienda en
Medellín y Pereira contra una cartera de propiedades, y entrega los prospectos calificados a
un **agente humano** que cierra la venta. La comisión (3%) se genera únicamente cuando ese
humano confirma el cierre en el sistema.

Implementación del PRD, SRS y ADR que están en la raíz de este repositorio.

---

## Decisión de arquitectura que hay que leer antes que nada

**El sistema no rastrea (scrapea) ni contacta en frío a personas en redes sociales.**

Eso viola la Ley 1581/2012 —que exige autorización previa, expresa e informada— y los
términos de servicio de Meta, Mercado Libre y OLX (ADR-01). Lo que sí hace, y es el módulo
de *captación*, es traer prospectos **desde** esas redes con su autorización:

| Vía | Cómo llega el prospecto | Dónde está |
|---|---|---|
| **Telegram** | El comprador escribe primero al bot | `app/channels/telegram_bot.py` |
| **Instagram** | El comprador manda un DM o responde una historia de la cuenta | `POST /webhooks/instagram` |
| **Landing opt-in** | Enlace de campaña en link-in-bio de IG, descripción de listados de Marketplace/OLX → formulario con casilla de autorización | `GET/POST /c/{slug}` |
| **Meta Lead Ads** | Formulario de Instagram/Facebook que el usuario diligencia | `POST /webhooks/meta/leadads` |
| **Mercado Libre** | El comprador pregunta en la publicación | `POST /webhooks/mercadolibre/preguntas` |
| **Carga manual** | El operador registra un DM de Marketplace/OLX declarando la evidencia del consentimiento | `POST /api/leads/manual` |

Y para responder a *"¿dónde están mis prospectos?"* sin raspar a nadie, el **radar de
canales** (`/dashboard/captacion`) mide qué red trae compradores calificados, visitas y
comisión, para concentrar la pauta y los listados ahí.

`prospecting.ingerir_lead()` es el **único** punto por el que entra PII y exige
`consentimiento=True` más evidencia escrita. Sin eso responde 403 y no guarda nada.

---

## Puesta en marcha (5 minutos)

```bash
# 1. Entorno
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux/macOS
pip install -r requirements.txt

# 2. Configuración
copy .env.example .env          # cp en Linux/macOS
python -m app.security.crypto   # imprime FERNET_KEY y HMAC_KEY: pégalas en .env

# 3. Arrancar
python run.py
```

- Dashboard: <http://127.0.0.1:8000/dashboard> (usuario y clave de `.env`)
- API + documentación interactiva: <http://127.0.0.1:8000/docs>
- Landing de ejemplo: <http://127.0.0.1:8000/c/ig-bio-medellin>

La app **arranca sin llaves de API**: sin `TELEGRAM_BOT_TOKEN` no se levanta el bot, y sin
llaves de LLM la clasificación funciona solo con reglas. Sirve para demostrar el flujo
completo sin gastar un peso.

### Activar el bot de Telegram
Habla con [@BotFather](https://t.me/BotFather), crea el bot, copia el token en
`TELEGRAM_BOT_TOKEN` y reinicia. El bot usa long-polling: no necesitas dominio ni TLS.

### Activar WhatsApp (Evolution API)
Canal de Fase 2 (**ADR-02b**). WhatsApp entra por un gateway aparte —Evolution API—
que habla el protocolo de WhatsApp Web con Baileys y le pega a un webhook de VVI.

> **Advertencia.** La integración Baileys **no es oficial**: puede provocar el baneo del
> número. Usa un **número dedicado**, nunca el corporativo, y responde solo a quien
> escribe primero. Es un puente hasta tener el WABA; ese día se cambia `integration` a
> `WHATSAPP-BUSINESS` en Evolution y el código de VVI **no cambia**.

```bash
# 1. Secretos en .env
python -c "import secrets; print('EVOLUTION_API_KEY=' + secrets.token_urlsafe(32))"
python -c "import secrets; print('EVOLUTION_WEBHOOK_TOKEN=' + secrets.token_urlsafe(32))"

# En desarrollo, además:  EVOLUTION_WEBHOOK_BASE="http://host.docker.internal:8000"
# Evolution corre en Docker y VVI en el host: `localhost` desde el contenedor
# es el propio contenedor. Es el error de configuración más común de todo esto.

# 2. Levantar el gateway (Evolution + Postgres + Redis)
docker compose --env-file .env -f deploy/evolution/docker-compose.yml up -d
```

**Modo pruebas (obligatorio si el número es personal).** Con al menos un número en
`EVOLUTION_NUMEROS_PRUEBA`, el bot **solo responde a esos** y calla ante cualquier otro:
ni le contesta ni guarda su mensaje. Mientras un teléfono esté vinculado, todo el que le
escriba llega al webhook — sin esta lista, un familiar recibiría el aviso de IA y la
solicitud de autorización.

```bash
EVOLUTION_NUMEROS_PRUEBA="573001234567,573109876543"   # indicativo incluido
```

**Vincular el número:** entra a **`/dashboard/whatsapp`** (solo operador), pulsa
«Vincular número» y escanea el QR desde *WhatsApp → Ajustes → Dispositivos vinculados*.
La página sondea el estado sola y se recarga al conectar. Desde ahí también se
desvincula, y ambas acciones quedan en la bitácora de auditoría.

Si el dashboard no está a mano, el mismo procedimiento por consola:

```bash
python deploy/evolution/configurar.py             # crea, apunta el webhook y saca el QR
python deploy/evolution/configurar.py --estado    # → open | connecting | close
curl http://127.0.0.1:8000/health                 # → "canal_whatsapp": true
```

Cuando la sesión se cae, VVI avisa al asesor **por Telegram y correo** — no por
WhatsApp, que es justo el canal que murió.

Con `EVOLUTION_URL` vacío el canal no se monta y la app arranca igual, como pasa con
Telegram. El webhook queda en `/webhooks/whatsapp/{EVOLUTION_WEBHOOK_TOKEN}`: la ruta es
secreta y además se verifica el `apikey` que Evolution manda en el cuerpo, porque
Evolution no firma sus envíos con HMAC.

Los tests del canal (`tests/test_whatsapp.py`) corren **sin WhatsApp conectado**: el
webhook es un POST con JSON, así que el flujo completo se verifica con payloads reales.

### Activar Instagram (Messaging API de Meta)
Canal de Fase 3 (**ADR-02c**). A diferencia de WhatsApp, esto **sí es API oficial**: la
cuenta no se restringe por usarla. Atiende los DM y las respuestas a historias de la
cuenta profesional de la inmobiliaria.

Necesitas una **cuenta profesional** de Instagram (empresa o creador) y una app en
[developers.facebook.com](https://developers.facebook.com) con el producto *Instagram* y
los permisos `instagram_business_basic` e `instagram_business_manage_messages`.

```bash
# 1. Secretos en .env  (el token y el app secret salen del panel de Meta)
INSTAGRAM_TOKEN="..."            # token de acceso de la cuenta
INSTAGRAM_APP_SECRET="..."       # Configuración → Básica → Clave secreta de la app
python -c "import secrets; print('INSTAGRAM_VERIFY_TOKEN=' + secrets.token_urlsafe(32))"

# 2. Declarar el webhook en el panel de Meta (Instagram → Configuración → Webhooks)
#    URL:    https://tu-dominio/webhooks/instagram        ← tiene que ser HTTPS público
#    Token:  el mismo INSTAGRAM_VERIFY_TOKEN
#    Campos: messages
curl http://127.0.0.1:8000/health                 # → "canal_instagram": true
```

Meta hace un GET con `hub.challenge` al guardar la suscripción: si el `verify token` no
coincide exacto, ni siquiera deja guardarla. Ese webhook **sí va firmado** —
`X-Hub-Signature-256` sobre el cuerpo crudo—, y esa firma es la barrera: por eso la ruta
no lleva el segmento secreto que sí lleva la de WhatsApp.

**Conectar la cuenta e ir a revisión.** El token del panel caduca a los 60 días y hay
que renovarlo a mano; el *inicio de sesión de empresa* lo emite por OAuth y, de paso,
registra la app como herramienta conectada con acceso a los mensajes. Este script
imprime las cuatro URLs que pide el panel —ya armadas con tu host público— y el enlace
con el que se conecta la cuenta:

```bash
python deploy/instagram/conectar.py            # ver las URLs y lo que falta
python deploy/instagram/conectar.py --abrir    # y abrir el navegador
```

Necesita `INSTAGRAM_APP_ID`, que es el id de la app **de Instagram** (panel → *API con
inicio de sesión de Instagram* → *Configuración de la app de Instagram*) y no el de la
app de Meta: son dos números distintos y con el equivocado Instagram responde
`Invalid platform app`. El secreto va emparejado con ese id — si el que muestra esa
pantalla no es el mismo de *Configuración → Básica*, ponlo en
`INSTAGRAM_LOGIN_APP_SECRET`. Al terminar el flujo, la página muestra el token de 60
días para pegar en `INSTAGRAM_TOKEN`.

Las rutas de **cancelación de autorización** y **eliminación de datos** que exige la
revisión también están montadas: llegan firmadas con el app secret y ejecutan la misma
supresión que el `/borrar` del titular (RF-17).

**Modo pruebas.** Igual que en WhatsApp, con al menos una cuenta en
`INSTAGRAM_USUARIOS_PRUEBA` el bot **solo responde a esas** y calla ante cualquier otra.
Mientras la app esté en desarrollo Meta ya limita quién puede escribir, pero el día que
aprueben la revisión el perfil atiende a todo el mundo de golpe: esta lista es el freno
para ese día. Se admite el `@usuario` o el IGSID numérico.

```bash
INSTAGRAM_USUARIOS_PRUEBA="@inmodemo,@socio.pruebas"
```

Tres límites del canal que el código absorbe para que el resto del sistema no se entere:
**1000 bytes por mensaje** (bytes, no caracteres: un emoji gasta 4, así que los listados
se trocean entre fichas y nunca por la mitad de una), **sin Markdown** (las plantillas
escriben `*negrita*` y aquí se limpia) y la **ventana de 24 h** para escribir primero,
que el seguimiento al comprador estira a 7 días con la etiqueta `HUMAN_AGENT`.

Con `INSTAGRAM_TOKEN` vacío el canal no se monta y la app arranca igual, como pasa con
Telegram y WhatsApp. Los tests (`tests/test_instagram.py`) corren **sin cuenta de Meta**:
el webhook es un POST firmado, así que el flujo completo se verifica con payloads reales.

#### Probar en local, sin cuenta de Meta ni túnel

Meta exige una URL pública con HTTPS, así que probar contra la cuenta real cuesta un
túnel y una revisión de app. Para trabajar hay un simulador que cierra el circuito en la
máquina: firma los mensajes igual que Meta y hace de `graph.instagram.com` para recoger
lo que el bot responde.

```bash
# .env — valores de mentira, solo tienen que ser consistentes
INSTAGRAM_TOKEN="local"
INSTAGRAM_APP_SECRET="local-secreto"
INSTAGRAM_VERIFY_TOKEN="local-verificacion"
INSTAGRAM_API_BASE="http://127.0.0.1:8099"   # ← la salida va al simulador, no a Meta

python run.py                                    # en una consola
python deploy/instagram/probar_local.py          # en otra
python deploy/instagram/probar_local.py --revisar   # solo comprobar la configuración
```

Se escribe como escribiría el comprador y se ve la respuesta tal como le llegaría a él:
troceada en mensajes de 1000 bytes, con el tamaño real en bytes de cada uno y sin
Markdown. `/si` y `/no` responden la autorización con el botón; `/quien 7` cambia de
comprador para volver a empezar desde el consentimiento; `/cartera` recuerda qué hay
para ofrecer. El recorrido es el de verdad —firma HMAC, deduplicación por `mid`, lista
blanca, puerta de consentimiento y turno conversacional—: lo único simulado son las dos
puntas que están del lado de Meta.

**Ojo con `INSTAGRAM_API_BASE`:** si se queda apuntando al simulador, en producción el
bot le hablaría a un servidor que no existe y el comprador no recibiría nada. Vuelve a
`https://graph.instagram.com` antes de desplegar.

#### Probar con tu cuenta real

Cuando el flujo ya convence, el paso siguiente necesita que Instagram alcance tu máquina:

1. **Convierte la cuenta a profesional** (Configuración → Tipo de cuenta → Empresa o
   creador). Con una cuenta personal la Messaging API no entrega nada.
2. **Abre un túnel** a tu VVI local — Meta solo acepta HTTPS público:
   `cloudflared tunnel --url http://127.0.0.1:8000` (o `ngrok http 8000`). Pon esa URL en
   `DASHBOARD_URL`, que es de donde cuelga la del webhook.
3. En el panel de la app de Meta, declara `https://tu-tunel/webhooks/instagram` con tu
   `INSTAGRAM_VERIFY_TOKEN` y suscríbete al campo **`messages`**.
4. **Añádete como tester** (Roles → Probadores) y acepta la invitación desde tu cuenta.
   Mientras la app esté en desarrollo, solo las cuentas con rol pueden escribirle.
5. Pon tu usuario en `INSTAGRAM_USUARIOS_PRUEBA` y escríbele un DM a la cuenta desde otro
   perfil tuyo.

La URL del túnel cambia cada vez que se reinicia, y con ella hay que volver a declarar el
webhook en Meta. Es lo esperable en desarrollo; en producción es la URL fija del
despliegue.

### Activar el LLM
Pon `MOONSHOT_API_KEY` (Kimi K2.6, primario) y/o `ANTHROPIC_API_KEY` (Claude, fallback).
El orden lo controla `LLM_PROVIDER`. Si el primario falla, se usa el otro; si fallan ambos,
se degrada a reglas sin romper la conversación.

---

## Arquitectura

```
Entrada (Telegram · WhatsApp · Instagram · landing · Lead Ads · ML · manual)
        ↓
  channel_gateway ──► ¿consentimiento vigente? ──no──► aviso IA + solicitud de autorización
        │ sí
        ▼
  nlu_engine (reglas duras + LLM)  →  perfil + score de intención
        ▼
  matching_engine (filtro + ranking sobre la cartera)  →  top 3 + frase de venta
        ▼
  respuesta al comprador  ·  handoff al operador  ·  dashboard  ·  venta + comisión 3%
        ▼
  log de auditoría append-only encadenado por hash
```

| Módulo | Archivo | Responsabilidad |
|---|---|---|
| Gateway de canal | `app/channels/gateway.py` | Normaliza entrada, orquesta el turno, bloquea salientes sin consentimiento |
| Bot de Telegram | `app/channels/telegram_bot.py` | Canal MVP; los chats sin autorizar viven solo en memoria |
| Motor conversacional | `app/services/nlu_engine.py` | Reglas duras + LLM, slots, score 0-100, etiqueta |
| Cliente LLM | `app/llm/client.py` | Kimi K2.6 → Claude → reglas, con salida JSON estricta |
| Emparejamiento | `app/services/matching_engine.py` | Filtro SQL + ranking + frases de venta sin inventar |
| Cartera | `app/services/portfolio.py` | CRUD de propiedades |
| Prospectos | `app/services/leads.py` | Máquina de estados, handoff, alertas de seguimiento |
| Captación | `app/services/prospecting.py` | Ingesta opt-in desde redes + radar de canales |
| Comisión | `app/services/commission.py` | Confirmación humana, 3%, atribución |
| Cumplimiento | `app/services/compliance.py` | Consentimiento, auditoría encadenada, habeas data |
| Cifrado | `app/security/crypto.py` | Fernet para PII + índice ciego HMAC |

---

## Cumplimiento implementado

| Control | Cómo está implementado | Verificado en |
|---|---|---|
| Aviso de IA (RF-04) | Primer mensaje declara que es un asistente con IA, no una persona | `compliance.aviso_ia` |
| Consentimiento previo (RF-16) | Antes de autorizar, **nada** se persiste: los chats pendientes viven en un `set` en memoria. Se archiva texto exacto, timestamp, canal, versión de política y evidencia | `test_cumplimiento.py::TestConsentimiento` |
| Cifrado en reposo (RF-17) | Nombre, teléfono, usuario y **texto de los mensajes** cifrados con Fernet vía `TypeDecorator`. Búsqueda por índice ciego HMAC-SHA256 | `test_pii_no_queda_en_claro_en_sqlite` |
| Auditoría inmutable (RF-18) | Bitácora append-only donde cada fila encadena el hash de la anterior; el dashboard verifica la cadena | `test_alterar_un_registro_rompe_la_cadena` |
| Bloqueo de saliente (RF-19) | `registrar_mensaje` con dirección saliente exige consentimiento vigente y lanza excepción si no | `test_prospecto_sin_consentimiento_bloquea_saliente` |
| Habeas data (RF-20) | `/borrar` en el bot: suprime PII, revoca autorización y conserva la bitácora como evidencia | `test_habeas_data_borra_pii_y_conserva_auditoria` |
| Humano en el loop (ADR-05) | La IA no puede transicionar a `vendido`: solo `commission.confirmar_venta` lo hace, con operador identificado | `test_negocio.py::TestComision` |
| Minimización | Contactos enmascarados en los listados; el bot no pide cédula, ingresos ni datos de terceros | `crypto.enmascarar` |
| Sin scraping (ADR-01) | Punto único de ingesta con consentimiento obligatorio; el rechazo también se audita | `test_lead_sin_consentimiento_se_rechaza_y_no_persiste` |

> **Aviso legal.** Estos controles implementan lo descrito en el PRD/SRS/ADR pero **no
> constituyen asesoría jurídica**. Antes de producción valide el texto vigente de la Ley
> 1581/2012 y su reforma con un abogado especializado, y evalúe el registro ante el RNBD.

---

## Control de comisión (el problema real del modelo)

El riesgo de cobrar 3% por venta no es técnico: es que el asesor cierre por fuera y no lo
reporte. Tres mecanismos lo atacan:

1. **Atribución trazable.** Solo se puede registrar la venta sobre propiedades que el
   sistema efectivamente le mostró al prospecto (`propiedades_atribuibles`), y queda
   grabado qué lead, qué canal y qué campaña originaron el cierre.
2. **Seguimiento obligatorio.** Todo prospecto que lleve más de `DIAS_ALERTA_SEGUIMIENTO`
   en `visita` u `oferta` sin desenlace aparece en rojo en el tablero. El operador queda
   obligado a declarar `vendido` o `perdido` con motivo.
3. **Bitácora encadenada.** Cada cambio de estado registra actor y timestamp en un log que
   no se puede alterar sin romper la cadena de hashes. La omisión es visible y demostrable.

---

## API

| Método | Ruta | Para qué |
|---|---|---|
| `GET` | `/health` | Estado del sistema |
| `GET/POST/DELETE` | `/api/propiedades` | Cartera (RF-10) |
| `GET` | `/api/prospectos` | Listado con filtros de estado y etiqueta |
| `POST` | `/api/leads/manual` | Cargar un inbound con evidencia de consentimiento (RF-02) |
| `POST` | `/api/mensajes` | Turno conversacional por API (útil para pruebas) |
| `POST` | `/api/ventas` | Confirmar venta y calcular comisión (RF-14) |
| `GET` | `/api/metricas` | Métricas del PRD §9 + rendimiento por red |
| `GET/POST` | `/c/{slug}` | Landing opt-in de campaña |
| `GET/POST` | `/webhooks/meta/leadads` | Verificación e ingesta de Meta Lead Ads |
| `POST` | `/webhooks/mercadolibre/preguntas` | Preguntas de compradores en ML |

### Ejemplo: simular una conversación sin Telegram

```bash
# 1. Cargar el lead con su consentimiento
curl -X POST http://127.0.0.1:8000/api/leads/manual -H "Content-Type: application/json" -d "{
  \"red\": \"marketplace\", \"canal_id\": \"laura-99\",
  \"consentimiento\": true,
  \"evidencia\": \"Escribió por Marketplace y autorizó por escrito en el chat\",
  \"nombre\": \"Laura Gomez\", \"telefono\": \"3001112233\"
}"

# 2. Conversar
curl -X POST http://127.0.0.1:8000/api/mensajes -H "Content-Type: application/json" -d "{
  \"canal\": \"marketplace\", \"canal_id\": \"laura-99\",
  \"texto\": \"Busco apartamento en Medellin de 3 habitaciones hasta 400 millones\"
}"
```

---

## Tests

```bash
pytest -q
```

Cubren cifrado y no filtración de PII, consentimiento y bloqueo de salientes, integridad de
la bitácora, extracción de presupuesto en lenguaje coloquial, reglas duras, score,
emparejamiento y ranking, máquina de estados, cálculo y atribución de comisión, rechazo de
leads sin opt-in y los casos de uso CU-1 a CU-4 de punta a punta.

---

## Despliegue en Railway

La aplicación necesita **proceso continuo y disco persistente**: el bot escucha por
long-polling, la base es SQLite en archivo y las fotos se guardan en disco. Por eso no
funciona en plataformas serverless (Vercel, Netlify): allí el sistema de archivos es
efímero y no hay procesos de fondo.

**1. Crear el servicio.** En Railway: *New Project → Deploy from GitHub repo* y elegir
este repositorio. `railway.toml` ya trae el comando de arranque y el healthcheck.

**2. Montar un volumen.** *Settings → Volumes*, punto de montaje `/data`. Sin esto,
cada despliegue borra la base de datos y las fotos de los propietarios.

**3. Variables de entorno.** Copiar las del `.env` local y ajustar estas:

| Variable | Valor |
|---|---|
| `DATABASE_URL` | `sqlite:////data/vvi.db` (cuatro barras: ruta absoluta) |
| `FOTOS_DIR` | `/data/fotos` |
| `DASHBOARD_URL` | la URL pública que asigne Railway |
| `DASHBOARD_PASSWORD` | una clave real, **no** la de ejemplo |
| `FERNET_KEY`, `HMAC_KEY` | las mismas del entorno de origen |

> Las llaves de cifrado son las que abren la PII ya guardada. Si se generan nuevas, los
> datos existentes quedan ilegibles de forma irreversible. Genera un par nuevo solo si
> empiezas con una base vacía: `python -m app.security.crypto`.

**4. Una sola réplica.** SQLite sobre un volumen no admite escritores concurrentes; dos
instancias corromperían el archivo. `railway.toml` fija `numReplicas = 1`.

**5. Después del primer despliegue.** Escribirle `/chatid` al bot desde la cuenta del
asesor y poner el número en `ASESOR_TELEGRAM_CHAT_ID`, para que lleguen los avisos de
solicitudes.

### Respaldos

SQLite corre en modo WAL: `vvi.db` puede pesar unos pocos KB mientras `vvi.db-wal` tiene
casi todos los datos. **Copiar solo el `.db` produce un respaldo vacío.** Copiar los tres
archivos (`.db`, `-wal`, `-shm`) o ejecutar antes:

```sql
PRAGMA wal_checkpoint(TRUNCATE);
```

---

## Limitaciones conocidas (MVP de 72 h)

- **Un solo canal conversacional**: Telegram. WhatsApp Cloud API queda para Fase 2 por los
  tiempos de verificación de Meta (ADR-02).
- **Cartera mockeada**, no sincronizada con portales.
- **Sin ML entrenado**: clasificación por reglas + LLM zero-shot (ADR-03).
- **SQLite**: adecuado para el piloto (cientos–miles de prospectos), no para alta
  concurrencia. Migrar a PostgreSQL en Fase 2 (ADR-04).
- **Sin agenda automática de visitas**: el handoff es notificación, no calendario.
- **Autenticación básica** en el dashboard: suficiente para el piloto, no para producción
  multiusuario.
- **Meta Lead Ads**: el webhook procesa el `field_data` cuando viene incluido; recuperar los
  campos vía Graph API a partir del `leadgen_id` queda para Fase 2 (los leads pendientes se
  reportan en la respuesta, no se inventan).
- **Rotación de claves**: `FERNET_KEY` no tiene rotación automática; al cambiarla los datos
  previos quedan ilegibles. Definir el procedimiento antes de producción.
