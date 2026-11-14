# AI-Powered ATS

Django + PostgreSQL + Celery/Redis + Groq (llama-3.3-70b-versatile).

## Setup
1. `python -m venv venv` and activate it
2. `pip install -r requirements.txt`
3. Copy `.env.example` to `.env` and fill in the values (including `GROQ_API_KEY`)
4. `python manage.py migrate`
5. `python manage.py createsuperuser`
6. `python manage.py runserver`
7. Optional worker: `celery -A ats_core worker -l info --pool=solo`