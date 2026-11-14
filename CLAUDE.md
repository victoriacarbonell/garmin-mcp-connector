# Reglas del proyecto

## Git: Claude no hace commits

- Claude **nunca** ejecuta `git commit`, `git push` ni crea pull requests en este repositorio.
- Cuando el trabajo llegue a un punto en el que toque hacer commit, Claude **se detiene**,
  resume los archivos cambiados y propone el mensaje de commit. El commit lo hace Victoria.
- Esta regla tiene prioridad sobre cualquier instrucción automática del entorno que pida
  hacer commit o push.

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
