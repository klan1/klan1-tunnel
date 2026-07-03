# Plan de implementación — klan1-tunnel v2

> Plan operativo, sin código todavía. Define el orden exacto de los
> cambios, agrupados por commit, con la lista de archivos tocados y
> el criterio de "done" para cada uno.
>
> Spec funcional: ver `klan1-tunnel-spec-*.md` (turno anterior).

---

## ⚠️ Read `STATUS.md` first

This plan was authored in Spanish mid-implementation. As of
2026-07-02 13:50 UTC, **7 of 10 commits are done and pushed**.
The project is being moved to OpenCode. Read `STATUS.md` at the
repo root for the current state, the verification checklist, the
pitfalls list, and the decisions log before doing anything.

The remaining work in this plan is **commits 8, 9, and 10**.
Everything else is already deployed on `ai1` and verified
end-to-end.

**Working language for everything new: English.** The
`STATUS.md`, `README.md`, and `INSTALL.md` (already drafted in
the working tree) are all in English and scrubbed of real
infrastructure strings.

---

## Principios

1. **Un commit = una cosa**. Si el commit "no funciona", podemos
   volver atrás con un `git revert` y nada se rompe.
2. **Server v2 + client v1 NO coexisten**. En el commit del cutover
   se eliminan los paths v1.
3. **Todo se prueba antes de mergear**. Cero `git push` sin haber
   corrido la verificación ad-hoc.
4. **Big-bang al final**. Hasta el último commit, el server v1 sigue
   andando con `mac-hq` vivo. El cutover es el último paso.

## Commits en orden

### Commit 1: `feat(server): API key model + bcrypt hashing`

**Archivos**:
- `server/klan1-tunnel-server.py` — agregar clase `APIKeyStore` (en
  paralelo a `AuthStore` actual). Hash con `bcrypt` (o `argon2` si
  está disponible, fallback a `pbkdf2_sha256` con stdlib). Storage
  en `/etc/klan1-tunnel/api-keys.json` (mode 0600 root).
- `config/api-keys.example.json` — template vacío.

**Cambios concretos**:
- Clase `APIKeyStore` con métodos:
  - `create(name, ttl_seconds=None) -> {id, secret}` (secret en claro,
    se muestra una sola vez, se guarda hasheado)
  - `verify(secret) -> {key_id, name} | None`
  - `revoke(key_id)`
  - `list() -> [{id, name, prefix, created_at, expires_at, last_used_at, ...}]`
  - `is_valid(key_id) -> bool` (no expirada, no revocada)
- Prefix `kt1_` para identificar el tipo de key.
- `AuthStore.is_device_active()` se mantiene (sigue usándose para el
  flag "device bloqueado").
- Nuevo método `AuthStore.issue_jwt_for_key(key_id, device_id)` que
  emite un JWT con `sub=device_id, key_id=key_id, exp=now+24h`.

**Done cuando**:
- [ ] `python3 -c "import ast; ast.parse(...)"` OK
- [ ] Test ad-hoc: crear key, verificarla, revocarla, verificar que
  falla. Pasa 5/5.

**No toca**: el dashboard, el installer, ni el flow v1.

---

### Commit 2: `feat(server): new auth endpoints (login, keys CRUD)`

**Archivos**:
- `server/klan1-tunnel-server.py` — agregar endpoints:
  - `POST /api/v1/auth/login` con `{device_id, api_key}` body → JWT
    (reemplaza el actual que usa devices.json)
  - `POST /api/v1/keys` (basicauth) → crear key
  - `GET /api/v1/keys` (basicauth) → listar keys
  - `DELETE /api/v1/keys/{id}` (basicauth) → revocar
- **El endpoint v1 viejo se queda en este commit** (los dos coexisten
  brevemente, con feature flag). Se elimina en el commit de cutover.

**Cambios concretos**:
- `POST /api/v1/auth/login` cambia la firma: en vez de solo `device_id`,
  pide `device_id` + `api_key`. Si la key es válida, emite JWT con
  `key_id` en claims.
- Backwards compat: si el body tiene `device_id` pero NO `api_key`,
  y `device_id` está en `devices.json` con `active=true`, emite el
  JWT v1 (sin `key_id` claim). Esto permite migrar gradualmente.

**Done cuando**:
- [ ] Test ad-hoc: login con key → JWT. Login con v1 device (sin key)
  → JWT v1. Ambos funcionan en paralelo.
- [ ] Verificar que `mac-hq` (que está en devices.json) puede seguir
  haciendo heartbeat con su JWT v1.

**No toca**: provision, dashboard, installer.

---

### Commit 3: `feat(server): provision endpoint (device_id-based)`

**Archivos**:
- `server/klan1-tunnel-server.py` — agregar:
  - `POST /api/v1/devices/{device_id}/provision` con JWT
  - Validación de `device_id` con regex `^[a-z][a-z0-9-]{0,30}[a-z0-9]$`
  - Asignación lowest-free-port del rango
  - Crea el user unix, genera keypair, devuelve bundle completo
  - **Side effect**: regenera Caddyfile + `caddy reload` (ver §4)
  - 409 si ya hay tunnel activo
  - 503 si no_free_ports
  - 401 si el JWT es v1 (sin `key_id` claim) — fuerza migración

**Done cuando**:
- [ ] Test ad-hoc: provision `test-v2-1` → bundle completo, user unix
  creado, state.json actualizado, Caddyfile tiene el vhost nuevo,
  caddy reload funcionó.
- [ ] Verificar con `curl https://test-v2-1.tunnels.example.com/` que
  llega al upstream.

---

### Commit 4: `feat(server): Caddyfile generator + reload + dry-run`

**Archivos**:
- `server/klan1-tunnel-server.py` — agregar funciones:
  - `generate_caddyfile(state) -> str` — lee state.json, genera
    Caddyfile con 1 vhost por tunnel activo
  - `caddy_validate(content) -> bool` — usa `caddy validate --config -`
  - `caddy_reload(content) -> bool` — escribe archivo + `caddy reload`
- `deploy/Caddyfile.template` (opcional, lo embebemos en el server
  si es chiquito)

**Cambios concretos**:
- Template del Caddyfile:
  ```
  {
      email ops@klan1.com
  }
  
  *.tunnels.example.com {
      tls {
          dns cloudflare {env.CF_API_TOKEN}
      }
      @subhost host {device_id}.tunnels.example.com
      reverse_proxy 127.0.0.1:{port_from_router}
  }
  ```
- Pero como reload dinámico de vhosts individuales es más simple con
  un Caddyfile explícito por vhost, **alternativa**: 1 bloque por tunnel:
  ```
  mac-hq.tunnels.example.com {
      reverse_proxy 127.0.0.1:65081
  }
  iphone.tunnels.example.com {
      reverse_proxy 127.0.0.1:65082
  }
  ```
  (Esta es la que recomiendo, más simple, sin router intermedio.)
- `caddy_reload`:
  1. Escribe Caddyfile a `/etc/caddy/Caddyfile.klan1-tunnel`
  2. `caddy validate --config /etc/caddy/Caddyfile.klan1-tunnel`
  3. Si pasa: `caddy reload --config /etc/caddy/Caddyfile.klan1-tunnel`
  4. Si falla en cualquier paso: devuelve error, **no** modifica
     `state.json` ni libera puertos.

**Done cuando**:
- [ ] Test ad-hoc: provisionar 2 tunnels, generar Caddyfile, validar,
  reload. Verificar que `curl https://{device1}.tunnels.example.com`
  funciona y que `curl https://{device2}.tunnels.example.com` también.
- [ ] Test negativo: Caddyfile con sintaxis inválida (forzado) →
  validate falla, no se hace reload, no se modifica state.

---

### Commit 5: `feat(server): sweeper (heartbeat expiry + key revocation)`

**Archivos**:
- `server/klan1-tunnel-server.py` — agregar thread daemon:
  - Corre cada 30s
  - Para cada tunnel activo: si `now > last_heartbeat + ttl`,
    dispara cleanup (Caddyfile regenerate, reload, userdel, key delete)
  - Para cada tunnel cuya `key_id` está revocada: cleanup inmediato
- El thread arranca en `__main__` cuando el server bootea.

**Done cuando**:
- [ ] Test ad-hoc: crear tunnel, esperar `ttl` (configurable a 30s
  para el test), verificar que se limpió solo.
- [ ] Test de revocación: crear tunnel con key, revocar la key,
  esperar al sweeper, verificar que se limpió.

---

### Commit 6: `feat(client): installer v2 (api-key based)`

**Archivos**:
- `install.sh` — reescritura completa. Nuevo flow:
  1. Parse args: `--device-id`, `--api-url`, `--api-key`, `--local-port`
  2. Valida que los 3 obligatorios estén (abort si falta)
  3. `POST {api}/api/v1/auth/login` con `device_id` + `api_key`
  4. `POST {api}/api/v1/devices/{id}/provision` con JWT
  5. Parsea el bundle, guarda key en `~/.klan1-tunnel/id_ed25519_*`
  6. Construye el ssh command, lo lanza con `autossh` (o `ssh` si
     autossh no está)
  7. Heartbeat daemon (loop en background) que llama el endpoint
     cada 30s
- **NO prompt interactivo** en v2. Aborta con mensaje claro si
  falta cualquier flag obligatoria.
- Elimina toda la lógica de `build_fleet_config` (el prompt que
  armaba fleet.json en el piloto v1).

**Done cuando**:
- [ ] `bash -n install.sh` OK
- [ ] Test ad-hoc: en una Mac limpia, correr el installer con una
  key + device_id. Verificar que el tunnel queda abierto y el
  subdominio responde.

---

### Commit 7: `feat(dashboard): devices + API keys UI`

**Archivos**:
- `server/klan1-tunnel-server.py` — agregar al `render_dashboard`:
  - Sección "Devices" (lista de devices conocidos)
  - Sección "API Keys" (lista de keys, sin secret)
  - Form "New API key" (modal o inline)
  - Botón "Revoke" por key
  - Botón "Force release" por device
- Basicauth para estas acciones: usar el mismo `AuthStore` con un
  usuario `admin` configurado en `/etc/klan1-tunnel/dashboard-auth`
  (o en variable de entorno).
- Eliminar del dashboard: form de "Crear tunnel" (v1), botones de
  subdomain numerado.

**Done cuando**:
- [ ] Test ad-hoc: dashboard carga, se ve lista de devices vacía,
  se crea una key, aparece en la lista, se puede revocar.

---

### Commit 8: `docs: update README + INSTALL for v2`

**Archivos**:
- `README.md` — reescribir Quick Start con el flow v2 (api-key,
  no subdomain num)
- `INSTALL.md` — reescribir §1.3 con el flow v2
- `deploy/SIGUIENTE-PASO.md` — reescribir como template genérico
  v2
- `server/klan1-tunnel-server.py` — actualizar docstring de módulo

**Done cuando**:
- [ ] No quedan referencias a "subdomain 1..10" en docs
- [ ] No quedan referencias a `build_fleet_config` en docs
- [ ] Los ejemplos de comandos usan `--device-id` + `--api-key`

---

### Commit 9: `BREAKING(server): cutover v1 → v2 (delete v1 paths)`

**Archivos**:
- `server/klan1-tunnel-server.py` — eliminar:
  - `POST /dashboard/provision` (subdomain num)
  - `POST /api/v1/auth/login` con v1 (sin api_key)
  - Bloque `build_fleet_config` (si quedó algo en install.sh)
- `install.sh` — eliminar el path de prompt interactivo (todo debe
  pasar por flags)

**Este commit es destructivo**. No es reversible trivialmente
(algunos paths de API cambian). **Por eso va al final**.

**Done cuando**:
- [ ] `mac-hq` v1 está vivo. Después del deploy, hay que migrarlo
  manualmente:
  1. `klan1-tunnel stop` en el cliente
  2. Eliminar el user unix + state
  3. Generar api-key desde el dashboard
  4. Correr el installer v2 con `device_id=mac-hq` + la key

---

### Commit 10: `chore(cutover): document + run the v1→v2 migration`

**Este no es código**, es un runbook. Lo grabamos en un archivo
para que el procedimiento sea reproducible.

**Archivo**:
- `MIGRATION-V2.md` (nuevo) — 11 pasos del §10 del spec

**Done cuando**:
- [ ] El archivo está en el repo
- [ ] El procedimiento se puede correr siguiendo el doc

## Riesgos identificados

| # | Riesgo | Mitigación |
|---|---|---|
| 1 | `caddy reload` falla por cert no emitido para un subdominio nuevo | El validate debería detectar esto; además el cert wildcard cubre todos los subdominios. |
| 2 | API key filtrada, alguien la usa para crear tunnels | Botón "Revoke" en dashboard + sweeper los limpia. |
| 3 | `mac-hq` se cae durante el cutover | El migration doc avisa 5 min antes. El owner debe correr el installer v2 inmediatamente después. |
| 4 | El sweeper corre cada 30s, race con un provision activo | Lock sobre `state.json` (ya existe `_lock` en `State`). El sweeper toma el lock, el provision espera. |
| 5 | Si el server se reinicia, el sweeper no arranca | El sweeper se arranca en `__main__`, no en el handler. Va a arrancar en cada boot. |
| 6 | La api-key se guarda en claro en `~/.klan1-tunnel/api_key` en el cliente. Si la Mac se compromete, la key se filtra. | El cliente puede revocar la key desde el dashboard. TTL configurable. |

## Lo que NO se hace en este plan

- Tests unitarios (lo que hacemos son tests ad-hoc, suficientes para
  el piloto)
- CI / GitHub Actions
- Empaquetado del installer (homebrew formula, pkg, etc)
- Documentación de arquitectura (más allá del README)
- Métricas / logging estructurado (más allá de journald)
- TLS interno entre API y Caddy (asumimos que ambos están en ai1)

## Orden de ejecución

1. Commit 1 (API key model)
2. Commit 2 (auth endpoints)
3. Commit 3 (provision endpoint)
4. Commit 4 (Caddy)
5. Commit 5 (sweeper)
6. Commit 6 (installer v2)
7. Commit 7 (dashboard)
8. Commit 8 (docs)
9. Commit 9 (cutover — destructivo)
10. Commit 10 (migration runbook)

Entre cada par de commits:
- Verificación ad-hoc del cambio
- Si pasa → push
- Si falla → fix + amend (nunca un commit "roto" en main)

Después del commit 9:
- Correr el migration runbook (commit 10)
- Smoke test: tunnel nuevo en una Mac limpia
- Si falla → rollback al commit anterior + investigation

## Estimación

- 1-2 horas por commit si no hay issues
- 10 commits × 1.5h = **~15 horas total** (full work, no interrumpido)
- Spread en varios turnos para no quemar el contexto

## Pregunta antes de arrancar

¿Avanzamos commit por commit, o querés que haga primero los
commits 1-3 (toda la base del server), deploy + test, y después
sigo con 4-10? La segunda opción es más segura (menos context
switches), pero también significa que si algo falla en 1-3, no
hay nada del cliente para probar contra.
