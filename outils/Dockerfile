# Interface web de sport.py. Le code n'est pas copié dans l'image : il est monté depuis le projet,
# une modification de sport.py est donc prise en compte au redémarrage du conteneur.
FROM python:3.12-slim

RUN pip install --no-cache-dir garminconnect==0.3.20 garmin-fit-sdk==21.218.0 fitdecode==0.11.0 pyyaml==6.0.2

# chemins identiques à ceux de l'ordinateur (voir SPORT_HOME et SPORT_PROJET dans endurain/.env)
ARG SPORT_HOME
ARG SPORT_PROJET
ENV PYTHONUNBUFFERED=1 \
    HOME=${SPORT_HOME}
USER 1000:1000
WORKDIR ${SPORT_PROJET}/outils
EXPOSE 8090
CMD ["python", "web.py"]
