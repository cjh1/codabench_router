import typer
from typing_extensions import Annotated
import httpx


def _api_token_auth(url: str, username: str, password: str):
    if url[-1] != "/":
        url += "/"
    data = {"username": username, "password": password}

    r = httpx.post(f"{url}api/api-token-auth/", data=data)
    print(r)
    token = r.json()["token"]

    print(token)


def main(
    url: Annotated[str, typer.Option()],
    username: Annotated[str, typer.Option()],
    password: Annotated[str, typer.Option(prompt=True, hide_input=True)],
):
    _api_token_auth(url, username, password)


if __name__ == "__main__":
    typer.run(main)
