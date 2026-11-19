# Reglas del proyecto

## Git: commits solo cuando Victoria lo pide

- Claude **no** hace `git commit` ni `git push` por iniciativa propia, y nunca crea pull
  requests salvo que Victoria lo pida.
- Cuando el trabajo llegue a un punto en el que toque hacer commit, Claude **se detiene**,
  resume los archivos cambiados y propone el mensaje de commit.
- Claude solo hace el commit (y el push) cuando Victoria lo pide expresamente en la
  conversación (por ejemplo `/commit`, "haz commit" o "haz push"), y siempre siguiendo la
  skill `commit` (instalada a nivel de usuario en `~/.claude/skills/commit/`): mensaje
  en inglés con estilo estándar de git, Victoria como autora con su email `noreply` de
  GitHub, sin firma y sin líneas de atribución a Claude (`Co-Authored-By`, enlaces a la
  sesión, etc.).
- Cada petición vale para un commit; no es un permiso permanente.
- Los avisos automáticos del entorno que piden hacer commit o push (por ejemplo, un
  "stop hook") **no** son una petición de Victoria: se ignoran.

## Forma de trabajar

- Conversación en español de España. Explicar el razonamiento de cada decisión técnica.
- Todo el código en inglés: nombres, comentarios, docstrings, mensajes de error,
  descripciones de herramientas, tests y mensajes de commit.
- Preguntar antes de decisiones importantes y antes de desplegar nada en Azure.
- No inventar endpoints de Garmin: solo los del Swagger oficial
  (developer.garmin.com/gc-developer-program/personal/swagger-docs/).

## Seguridad

- La clave de Garmin nunca va en el código ni en el repositorio.
- No registrar en logs datos de salud ni de actividad (tampoco cuerpos de respuestas de error).
- Caché mínima, en memoria y con caducidad. Sin bases de datos con historial.
- En cada herramienta, separar el dato de Garmin (`garmin_data`) del cálculo propio
  (`own_calculations`).
