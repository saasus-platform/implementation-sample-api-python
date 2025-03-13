import os
import uvicorn
from typing import Union, Optional
from fastapi import FastAPI, Request, Response, Depends, HTTPException, Query
from pydantic import BaseModel
from starlette.middleware.cors import CORSMiddleware

from saasus_sdk_python.src.auth import SaasUserApi, TenantApi, TenantUserApi, TenantAttributeApi, UserAttributeApi, RoleApi, CreateSaasUserParam, CreateTenantUserParam, CreateTenantUserRolesParam, TenantProps
from saasus_sdk_python.src.pricing import PricingPlansApi
from saasus_sdk_python.callback.callback import Callback
from saasus_sdk_python.middleware.middleware import Authenticate
from saasus_sdk_python.client.auth_client import SignedAuthApiClient
from saasus_sdk_python.client.pricing_client import SignedPricingApiClient
from sqlalchemy import create_engine, Column, Integer, String, TIMESTAMP, select
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.sql import func

from dotenv import load_dotenv

DATABASE_URL = os.getenv("DATABASE_URL")

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

load_dotenv()
app = FastAPI()
auth = Authenticate()
callback = Callback()
# ApiClientを継承したSignedAuthApiClientを使う
api_client = SignedAuthApiClient()
# ApiClientを継承したSignedPricingApiClientを使う
pricing_api_client = SignedPricingApiClient()

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

# DB依存性注入
def get_db():
    db = SessionLocal()
    try:
        yield db
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


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


# ユーザー登録用のPydanticモデルを定義
class UserRegisterRequest(BaseModel):
    email: str
    password: str
    tenantId: str
    userAttributeValues: Optional[dict] = None


# ユーザー登録
@app.post("/user_register")
async def user_register(request: UserRegisterRequest, auth_user: dict = Depends(fastapi_auth)):
    # リクエストデータの取得
    email = request.email
    password = request.password
    tenant_id = request.tenantId
    user_attribute_values = request.userAttributeValues

    if not auth_user.tenants:
        raise HTTPException(status_code=400, detail="No tenants found for the user")

    is_belonging_tenant = belonging_tenant(auth_user.tenants, tenant_id)
    if not is_belonging_tenant:
        raise HTTPException(status_code=400, detail="Tenant that does not belong")

    # ユーザー登録処理
    try:
        # ユーザー属性情報を取得
        user_attributes_obj = get_user_attributes()

        # ユーザー属性情報でnumber型が定義されている場合は、置換する
        if user_attribute_values is None:
            user_attribute_values = []
        else:
            user_attributes = user_attributes_obj.user_attributes
            for attribute in user_attributes:
                attribute_name = attribute.attribute_name
                attribute_type = attribute.attribute_type.value

                if attribute_name in user_attribute_values:
                    if attribute_type == "number":
                        user_attribute_values[attribute_name] = int(user_attribute_values[attribute_name])

        # SaaSユーザー登録用パラメータを作成
        create_saas_user_param = CreateSaasUserParam(email=email, password=password)

        # SaaSユーザーを登録
        SaasUserApi(api_client=api_client).create_saas_user(create_saas_user_param=create_saas_user_param)

        # テナントユーザー登録用のパラメータを作成
        create_tenant_user_param = CreateTenantUserParam(email=email, attributes=user_attribute_values)

        # 作成したSaaSユーザーをテナントユーザーに追加
        tenant_user = TenantUserApi(api_client=api_client).create_tenant_user(tenant_id=tenant_id, create_tenant_user_param=create_tenant_user_param)

        # テナントに定義されたロール一覧を取得
        roles_obj = RoleApi(api_client=api_client).get_roles()

        # 初期値はadmin（SaaS管理者）とする
        add_role = "admin"

        for role in roles_obj.roles:
            # userが定義されていれば、設定するロールをuserにする
            if role.role_name == "user":
                add_role = role.role_name
                break

        # ロール設定用のパラメータを作成
        create_tenant_user_roles_param = CreateTenantUserRolesParam(role_names=[add_role])

        # 作成したテナントユーザーにロールを設定
        TenantUserApi(api_client=api_client).create_tenant_user_roles(tenant_id=tenant_id, user_id=tenant_user.id, env_id=3, create_tenant_user_roles_param=create_tenant_user_roles_param)

        return {"message": "User registered successfully"}

    except Exception as e:
        print(e)
        raise HTTPException(status_code=500, detail=str(e))


class UserDeleteRequest(BaseModel):
    tenantId: str
    userId: str

class DeleteUserLog(Base):
    __tablename__ = "delete_user_log"
    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(String(100), nullable=False)
    user_id = Column(String(100), nullable=False)
    email = Column(String(100), nullable=False)
    delete_at = Column(TIMESTAMP, server_default=func.current_timestamp())


class DeleteUserLogResponse(BaseModel):
    id: int
    tenant_id: str
    user_id: str
    email: str
    delete_at: Optional[str]


@app.delete("/user_delete")
def user_delete(request: UserDeleteRequest, auth_user: dict = Depends(fastapi_auth)):
    # リクエストデータの取得
    tenant_id = request.tenantId
    user_id = request.userId

    if not auth_user.tenants:
        raise HTTPException(status_code=400, detail="No tenants found for the user")

    is_belonging_tenant = belonging_tenant(auth_user.tenants, tenant_id)
    if not is_belonging_tenant:
        raise HTTPException(status_code=400, detail="Tenant that does not belong")

    try:
        # ユーザー削除ログにメールアドレスを登録するため、SaaSusからユーザー情報を取得
        delete_user = TenantUserApi(api_client=api_client).get_tenant_user(tenant_id=tenant_id, user_id=user_id)

        # テナントからユーザー情報を削除
        TenantUserApi(api_client=api_client).delete_tenant_user(tenant_id=tenant_id, user_id=user_id)

        # ユーザー削除ログを設定
        delete_user_log = DeleteUserLog(tenant_id=tenant_id, user_id=user_id, email=delete_user.email)

        # 登録実行
        db = SessionLocal()
        try:
            db.add(delete_user_log)
            db.commit()
            db.refresh(delete_user_log)
        except Exception as e:
            print(e)
            db.rollback()
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            db.close()

        return {"message": "User delete successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ユーザー削除ログを取得
@app.get("/delete_user_log", response_model=list[DeleteUserLogResponse])
def get_delete_user_logs(tenant_id: str, auth_user: dict = Depends(fastapi_auth), db: Session = Depends(get_db)):
    if not auth_user.tenants:
        raise HTTPException(status_code=400, detail="No tenants found for the user")

    is_belonging_tenant = belonging_tenant(auth_user.tenants, tenant_id)
    if not is_belonging_tenant:
        raise HTTPException(status_code=400, detail="Tenant that does not belong")

    try:
        # ユーザー削除ログを取得
        query = select(DeleteUserLog).where(DeleteUserLog.tenant_id == tenant_id)
        result = db.execute(query).scalars().all()

        # SQLAlchemyのオブジェクトをPydanticモデルに変換
        response_data = [
            DeleteUserLogResponse(
                id=log.id,
                tenant_id=log.tenant_id,
                user_id=log.user_id,
                email=log.email,
                delete_at=log.delete_at.isoformat() if log.delete_at else None
            )
            for log in result
        ]

        return response_data

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# 料金プランを取得
@app.get("/pricing_plan")
def get_pricing_plan(auth_user: dict = Depends(fastapi_auth), plan_id: Optional[str] = Query(None)):
    if not auth_user.tenants:
        raise HTTPException(status_code=400, detail="No tenants found for the user")

    # クエリパラメータでテナントIDが渡されていない場合はエラー
    if not plan_id:
        raise HTTPException(status_code=400, detail="No price plan found for the tenant")

    try:
        plan = PricingPlansApi(api_client=pricing_api_client).get_pricing_plan(plan_id=plan_id)

        return plan

    except Exception as e:
        print(e)
        raise HTTPException(status_code=500, detail=str(e))

# テナント属性情報を取得
@app.get("/tenant_attributes_list")
def get_tenant_attributes_list(auth_user: dict = Depends(fastapi_auth)):
    try:
        tenant_attributes = TenantAttributeApi(api_client=api_client).get_tenant_attributes()

        return tenant_attributes
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class SelfSignupRequest(BaseModel):
    tenantName: str
    tenantAttributeValues: Optional[dict] = None
    userAttributeValues: Optional[dict] = None


# セルフサインアップ
@app.post("/self_sign_up")
async def self_signup(request: SelfSignupRequest, auth_user: dict = Depends(fastapi_auth)):

    if auth_user.tenants:
        raise HTTPException(status_code=400, detail="User is already associated with a tenant")

    # リクエストデータの取得
    tenant_name = request.tenantName
    tenant_attribute_values = request.tenantAttributeValues
    user_attribute_values = request.userAttributeValues
    # ユーザー属性情報の取得
    user_attributes_obj = get_user_attributes()
    try:
        # テナント属性情報の取得
        tenant_attributes_obj = get_tenant_attributes_list()
        # テナント属性情報で number 型が定義されている場合は置換する
        if tenant_attribute_values is None:
            tenant_attribute_values = {}
        else:
            tenant_attributes = tenant_attributes_obj.tenant_attributes
            for attribute in tenant_attributes:
                attribute_name = attribute.attribute_name
                attribute_type = attribute.attribute_type.value

                if attribute_name in tenant_attribute_values:
                    if attribute_type == "number":
                        tenant_attribute_values[attribute_name] = int(tenant_attribute_values[attribute_name])

        # `TenantProps` のインスタンスを作成
        tenant_props = TenantProps(
            name=tenant_name,
            attributes=tenant_attribute_values,
            back_office_staff_email=auth_user.email  # 現在のユーザーのメールアドレスを利用
        )

        # テナントを作成
        tenant_api = TenantApi(api_client=api_client)
        created_tenant = tenant_api.create_tenant(body=tenant_props)

        # 作成したテナントのIDを取得
        tenant_id = created_tenant.id

        # ユーザー属性情報の取得
        user_attributes_obj = get_user_attributes()

        # ユーザー属性情報で number 型が定義されている場合は置換する
        if user_attribute_values is None:
            user_attribute_values = {}
        else:
            user_attributes = user_attributes_obj.user_attributes
            for attribute in user_attributes:
                attribute_name = attribute.attribute_name
                attribute_type = attribute.attribute_type.value

                if attribute_name in user_attribute_values:
                    if attribute_type == "number":
                        user_attribute_values[attribute_name] = int(user_attribute_values[attribute_name])

        # テナントユーザー登録用のパラメータを作成
        create_tenant_user_param = CreateTenantUserParam(
            email=auth_user.email,  # 登録者自身のメールアドレス
            attributes=user_attribute_values
        )

        # SaaSユーザーをテナントユーザーに追加
        tenant_user = TenantUserApi(api_client=api_client).create_tenant_user(
            tenant_id=tenant_id,
            create_tenant_user_param=create_tenant_user_param
        )

        # ロール設定用のパラメータを作成
        create_tenant_user_roles_param = CreateTenantUserRolesParam(role_names=["admin"])

        # 作成したテナントユーザーにロールを設定
        TenantUserApi(api_client=api_client).create_tenant_user_roles(
            tenant_id=tenant_id,
            user_id=tenant_user.id,
            env_id=3,
            create_tenant_user_roles_param=create_tenant_user_roles_param
        )

        return {"message": "User successfully signed up to the tenant"}

    except Exception as e:
        print(e)
        raise HTTPException(status_code=500, detail=str(e))

# リフレッシュトークンからIDトークンを取得する
@app.get("/refresh")
def refresh(request: Request):
    # クッキーから SaaSusRefreshToken を取得
    saasus_refresh_token = request.cookies.get("SaaSusRefreshToken")
    if not saasus_refresh_token:
        raise HTTPException(status_code=400, detail="SaaSusRefreshToken is missing")

    try:
        # refresh_token を使って新しい認証情報を取得
        credentials = callback.get_refresh_token_auth_credentials(saasus_refresh_token)

        return credentials
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ログアウト
@app.post("/logout")
def logout(response: Response):
    # クライアントのクッキーを削除する
    response.delete_cookie("SaaSusRefreshToken")
    
    return {"message": "Logged out successfully"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=80)
