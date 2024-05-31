import uvicorn
from typing import Union
from fastapi import FastAPI, Request, Depends, HTTPException
from starlette.middleware.cors import CORSMiddleware

from saasus_sdk_python.src.auth import TenantUserApi
from saasus_sdk_python.callback.callback import Callback
from saasus_sdk_python.middleware.middleware import Authenticate
from saasus_sdk_python.client.auth_client import SignedAuthApiClient

from dotenv import load_dotenv

load_dotenv()
app = FastAPI()
auth = Authenticate()
callback = Callback()
# ApiClientを継承したSignedApiClientを使う
auth_api_client = SignedAuthApiClient()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# FastAPI用の認証メソッド
def fastapi_auth(request: Request) -> Union[dict, HTTPException]:
    auth_header = request.headers.get("Authorization", "")
    token = auth_header.replace("Bearer ", "") if "Bearer " in auth_header else ""
    referer = request.headers.get("Referer", "")
    x_saasus_referer = request.headers.get("x_saasus_referer", "")
    user_info, error = auth.authenticate(id_token=token, referer=referer, x_saasus_referer=x_saasus_referer)
    if error:
        raise HTTPException(status_code=401, detail=str(error))
    return user_info


# 一時コードを取得する
def get_temp_code(request: Request):
    code = request.query_params.get("code")
    if not code:
        raise HTTPException(status_code=400, detail="code is not provided by query parameter")
    return code


@app.get("/credentials")
def get_credentials(request: Request):
    return callback.callback_route_function(get_temp_code(request))


@app.get("/userinfo")
def get_user_info(user_info: dict = Depends(fastapi_auth)):
    return user_info


@app.get("/users")
def get_tenant_users(auth_user: dict = Depends(fastapi_auth)):
    if not auth_user.tenants:
        raise HTTPException(status_code=400, detail="No tenants found for the user")

    tenant_id = auth_user.tenants[0].id

    try:
        tenant_user_info = TenantUserApi(api_client=auth_api_client).get_tenant_users(tenant_id=tenant_id,
                                                                                      _headers=auth_api_client.configuration.default_headers)
        return tenant_user_info.users
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=80)