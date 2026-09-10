import React from 'react';
import { Button, Input, Modal, QRCode, Select, Space, Typography } from 'antd';
import type { DeveloperApp, LoginFlow } from '../types';

export type AuthorizationLoginForm = {
  role: string; method: string; developer_app_id: number; code: string; password_2fa: string;
};
type Props = {
  loginOpen: boolean; closeLoginModal: () => void;
  loginForm: AuthorizationLoginForm;
  setLoginForm: React.Dispatch<React.SetStateAction<AuthorizationLoginForm>>;
  developerApps: DeveloperApp[]; loginFlow: LoginFlow | null; loginLoading: boolean;
  startStandbyLogin: () => Promise<void>; verifyStandbyLogin: () => Promise<void>;
  checkQrLogin: () => Promise<void>;
};

export function AuthorizationLoginModal(props: Props) {
  const { loginOpen, closeLoginModal, loginForm, setLoginForm, developerApps,
    loginFlow, loginLoading, startStandbyLogin } = props;
  return (
      <Modal
        title="新增备用授权"
        open={loginOpen}
        onCancel={closeLoginModal}
        footer={null}
        destroyOnHidden
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Select
            value={loginForm.role}
            onChange={(role) => setLoginForm({ ...loginForm, role })}
            options={[
              { value: 'standby_1', label: '备用授权 1' },
            ]}
          />
          <Select
            value={loginForm.developer_app_id || undefined}
            placeholder="选择开发者应用"
            onChange={(developer_app_id) => setLoginForm({ ...loginForm, developer_app_id })}
            options={developerApps.map((app) => ({ value: app.id, label: `${app.app_name} / ${app.health_status}` }))}
          />
          <Typography.Text type="secondary">通过当前服务器直连登录。</Typography.Text>
          <Select
            value={loginForm.method}
            onChange={(method) => setLoginForm({ ...loginForm, method })}
            options={[{ value: 'code', label: '验证码登录' }, { value: 'qr', label: '扫码登录' }]}
          />
          {!loginFlow && (
            <Button
              type="primary"
              loading={loginLoading}
              disabled={!loginForm.developer_app_id}
              onClick={startStandbyLogin}
            >
              发起登录
            </Button>
          )}
          <LoginVerification {...props} />
        </Space>
      </Modal>
  );
}

function LoginVerification(props: Props) {
  const { loginFlow, loginForm, setLoginForm, loginLoading, verifyStandbyLogin, checkQrLogin } = props;
  return <>
          {loginFlow && loginForm.method === 'code' && (
            <>
              <Input
                value={loginForm.code}
                placeholder="验证码"
                onChange={(event) => setLoginForm({ ...loginForm, code: event.target.value })}
                onPressEnter={verifyStandbyLogin}
              />
              <Input.Password
                value={loginForm.password_2fa}
                placeholder="2FA 密码（如需要）"
                onChange={(event) => setLoginForm({ ...loginForm, password_2fa: event.target.value })}
                onPressEnter={verifyStandbyLogin}
              />
              <Button type="primary" loading={loginLoading} onClick={verifyStandbyLogin}>完成备用授权登录</Button>
            </>
          )}
          {loginFlow && loginForm.method === 'qr' && (
            <>
              {loginFlow.qr_payload && (
                <div className="qr-login-preview">
                  <QRCode value={loginFlow.qr_payload} />
                </div>
              )}
              <Typography.Text type="secondary">二维码仅用于当前备用授权确认，不展示原始扫码内容。</Typography.Text>
              <Button type="primary" loading={loginLoading} onClick={checkQrLogin}>我已扫码，检查登录</Button>
            </>
          )}
  </>;
}
