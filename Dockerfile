FROM python:3.12

COPY . /app/

RUN pip install /app/

RUN rm -rf /app/

ENTRYPOINT [ "cr" ]