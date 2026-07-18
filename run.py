"""Запуск дев-сервера: py run.py  (или из .venv). Прод — через uvicorn в docker."""
import uvicorn

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
