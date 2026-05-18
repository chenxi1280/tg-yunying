import { API_BASE, api } from '../../shared/api/client';
import type { CaptchaChallenge, CaptchaVerifyResponse, CurrentUser } from '../types';

interface AuthActionParams {
  captchaChallenge: CaptchaChallenge | null;
  captchaInput: string;
  captchaToken: string;
  loginEmail: string;
  loginPassword: string;
  registerForm: { name: string; email: string; phone: string; password: string };
  changePasswordForm: { current_password: string; new_password: string; confirm_password: string };
  setAuthMode: (mode: 'login' | 'register') => void;
  setBusy: (busy: string) => void;
  setCaptchaChallenge: (challenge: CaptchaChallenge | null) => void;
  setCaptchaError: (error: string) => void;
  setCaptchaInput: (value: string) => void;
  setCaptchaLoading: (loading: boolean) => void;
  setCaptchaToken: (token: string) => void;
  setChangePasswordForm: (form: { current_password: string; new_password: string; confirm_password: string }) => void;
  setCurrentUser: (user: CurrentUser | null) => void;
  setNotice: (notice: string) => void;
  setToken: (token: string) => void;
  closeModal: () => void;
  handleActionError: (error: unknown) => void;
  showResult: (title: string, detail: string) => void;
}

export function createAuthActions(params: AuthActionParams) {
  async function refreshCaptchaChallenge() {
    params.setCaptchaLoading(true);
    params.setCaptchaError('');
    params.setCaptchaToken('');
    try {
      const challenge = await api<CaptchaChallenge>('/auth/captcha/challenge');
      params.setCaptchaChallenge(challenge);
      params.setCaptchaInput('');
    } catch (error) {
      params.setCaptchaChallenge(null);
      params.setCaptchaError('验证码加载失败，请刷新重试');
    } finally {
      params.setCaptchaLoading(false);
    }
  }

  async function requestCaptchaToken(): Promise<string | null> {
    if (!params.captchaChallenge) {
      params.setCaptchaError('请先刷新验证码');
      return null;
    }
    if (params.captchaInput.trim().length < 5) {
      params.setCaptchaError('请输入图片中的数字和字母');
      return null;
    }
    params.setCaptchaLoading(true);
    params.setCaptchaError('');
    params.setCaptchaToken('');
    try {
      const captcha = await api<CaptchaVerifyResponse>('/auth/captcha/verify', {
        method: 'POST',
        body: JSON.stringify({
          challenge_id: params.captchaChallenge.challenge_id,
          captcha_value: params.captchaInput,
        }),
      });
      params.setCaptchaToken(captcha.captcha_token);
      return captcha.captcha_token;
    } catch (error) {
      params.setCaptchaError('验证码验证失败，请重新输入');
      return null;
    } finally {
      params.setCaptchaLoading(false);
    }
  }

  async function verifyCaptcha() {
    await requestCaptchaToken();
  }

  async function login() {
    const captchaToken = params.captchaToken || await requestCaptchaToken();
    if (!captchaToken) {
      params.setNotice('请先输入正确的验证码');
      return;
    }
    params.setBusy('登录');
    params.setNotice('');
    const response = await fetch(`${API_BASE}/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        identifier: params.loginEmail,
        email: params.loginEmail,
        password: params.loginPassword,
        captcha_token: captchaToken,
      }),
    });
    if (!response.ok) {
      params.setBusy('');
      params.setNotice('登录失败，请检查账号和密码');
      await refreshCaptchaChallenge();
      return;
    }
    const data = await response.json();
    localStorage.setItem('tg_ops_token', data.access_token);
    params.setToken(data.access_token);
    params.setCurrentUser(data.user);
    params.setNotice('');
    params.setBusy('');
  }

  async function register() {
    if (!params.captchaToken) {
      params.setNotice('请先完成验证码验证');
      return;
    }
    params.setBusy('注册');
    params.setNotice('');
    try {
      const response = await fetch(`${API_BASE}/auth/register`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...params.registerForm, captcha_token: params.captchaToken }),
      });
      if (!response.ok) {
        params.setBusy('');
        params.setNotice('注册失败，请检查填写信息');
        await refreshCaptchaChallenge();
        return;
      }
      const data = await response.json();
      localStorage.setItem('tg_ops_token', data.access_token);
      params.setToken(data.access_token);
      params.setCurrentUser(data.user);
      params.setAuthMode('login');
      params.setNotice('注册成功，已进入运营管理平台。');
    } finally {
      params.setBusy('');
    }
  }

  async function changePassword() {
    if (params.changePasswordForm.new_password !== params.changePasswordForm.confirm_password) {
      params.setNotice('两次输入的新密码不一致');
      return;
    }
    params.setBusy('修改密码');
    try {
      const user = await api<CurrentUser>('/auth/change-password', {
        method: 'POST',
        body: JSON.stringify({
          current_password: params.changePasswordForm.current_password,
          new_password: params.changePasswordForm.new_password,
        }),
      });
      params.setCurrentUser(user);
      params.setChangePasswordForm({ current_password: '', new_password: '', confirm_password: '' });
      params.closeModal();
      params.showResult('密码已修改', '下次登录请使用新密码。');
    } catch (error) {
      params.handleActionError(error);
    } finally {
      params.setBusy('');
    }
  }

  function logout() {
    localStorage.removeItem('tg_ops_token');
    params.setToken('');
    params.setCurrentUser(null);
    params.setNotice('');
  }

  return {
    refreshCaptchaChallenge,
    verifyCaptcha,
    login,
    register,
    changePassword,
    logout,
  };
}
