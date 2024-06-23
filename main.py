import uvicorn
from typing import Union, Optional
from fastapi import FastAPI, Request, Depends, HTTPException, Header, Query
from starlette.middleware.cors import CORSMiddleware

from saasus_sdk_python import TenantUserApi
from saasus_sdk_python import TenantApi
from saasus_sdk_python import TenantAttributeApi
from saasus_sdk_python import UserAttributeApi
from saasus_sdk_python.callback.callback import Callback
from saasus_sdk_python.middleware.middleware import Authenticate
from saasus_sdk_python.client.client import SignedApiClient

from dotenv import load_dotenv

load_dotenv()
app = FastAPI()
auth = Authenticate()
callback = Callback()
# ApiClientを継承したSignedApiClientを使う
api_client = SignedApiClient()

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
    user_info, error = auth.authenticate(id_token=token, referer=referer)
    if error:
        raise HTTPException(status_code=401, detail=str(error))
    return user_info


# 一時コードを取得する
def get_temp_code(request: Request):
    code = request.query_params.get("code")
    if not code:
        raise HTTPException(status_code=400, detail="code is not provided by query parameter")
    return code


# ユーザーが所属しているテナントか確認する
def belonging_tenant(tenants: dict, tenant_id: str):
    is_belonging_tenant = False
    for tenant in tenants:
        if tenant.id == tenant_id:
            is_belonging_tenant = True
            break

    return is_belonging_tenant


@app.get("/credentials")
def get_credentials(request: Request):
    return callback.callback_route_function(get_temp_code(request))


@app.get("/userinfo")
def get_user_info(user_info: dict = Depends(fastapi_auth)):
    return user_info


@app.get("/users")
def get_tenant_users(auth_user: dict = Depends(fastapi_auth), tenant_id: Optional[str] = Query(None)):
    if not auth_user.tenants:
        raise HTTPException(status_code=400, detail="No tenants found for the user")

    # クエリパラメータでテナントIDが渡されていない場合はエラー
    if not tenant_id:
        raise HTTPException(status_code=400, detail="No tenant found for the user")

    # ユーザーが所属しているテナントか確認する
    is_belonging_tenant = belonging_tenant(auth_user.tenants, tenant_id)
    if not is_belonging_tenant:
        raise HTTPException(status_code=400, detail="Tenant that does not belong")

    try:
        tenant_user_info = TenantUserApi(api_client=api_client).get_tenant_users(tenant_id=tenant_id,
                                                                                 _headers=api_client.configuration.default_headers)

        return tenant_user_info.users
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/tenant_attributes")
def get_tenant_info(tenant_id: str, auth_user: dict = Depends(fastapi_auth)):
    if not auth_user.tenants:
        raise HTTPException(status_code=400, detail="No tenants found for the user")

    # ユーザーが所属しているテナントか確認する
    is_belonging_tenant = belonging_tenant(auth_user.tenants, tenant_id)
    if not is_belonging_tenant:
        raise HTTPException(status_code=400, detail="Tenant that does not belong")

    # テナント属性情報とテナント情報を取得
    try:
        tenant_attributes = TenantAttributeApi(api_client=api_client).get_tenant_attributes().to_dict()

        tenant_info = TenantApi(api_client=api_client).get_tenant(tenant_id=tenant_id)

        result = dict()
        for tenant_attribute in tenant_attributes['tenant_attributes']:
            detail = {
                tenant_attribute['attribute_name']: {
                    'display_name': tenant_attribute['display_name'],
                    'attribute_type': tenant_attribute['attribute_type'],
                    'value': tenant_info.attributes.get(tenant_attribute['attribute_name'], None)
                }
            }

            result.update(detail)

        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ユーザー属性情報を取得
@app.get("/user_attributes")
def get_user_attributes(auth_user: dict = Depends(fastapi_auth)):
    try:
        user_attributes = UserAttributeApi(api_client=api_client).get_user_attributes()

        return user_attributes
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=80)
