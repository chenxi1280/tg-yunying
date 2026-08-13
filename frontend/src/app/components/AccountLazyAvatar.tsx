import React from 'react';
import { Avatar, Space, Typography } from 'antd';

type AvatarLoadState = 'waiting' | 'loading' | 'loaded' | 'failed';

interface Props {
  displayName: string;
  hasAvatar: boolean;
  previewUrl: string;
  resolveUrl: (value: string) => string;
}

interface AccountIdentityCellProps extends Props {
  phone: string;
  poolName: string;
  username?: string | null;
  tgFirstName?: string | null;
  tgLastName?: string | null;
}

function statusLabel(state: AvatarLoadState) {
  if (state === 'waiting') return '有头像';
  if (state === 'loading') return '加载中';
  if (state === 'loaded') return '头像已加载';
  return '加载失败';
}

export function AccountLazyAvatar({ displayName, hasAvatar, previewUrl, resolveUrl }: Props) {
  const containerRef = React.useRef<HTMLDivElement>(null);
  const [loadState, setLoadState] = React.useState<AvatarLoadState>('waiting');
  const imageUrl = hasAvatar && previewUrl ? resolveUrl(previewUrl) : '';

  React.useEffect(() => {
    setLoadState('waiting');
    if (!imageUrl || !containerRef.current) return;

    const observer = new IntersectionObserver(([entry]) => {
      if (!entry.isIntersecting) return;
      setLoadState('loading');
      observer.disconnect();
    });
    observer.observe(containerRef.current);
    return () => observer.disconnect();
  }, [imageUrl]);

  if (!hasAvatar) {
    return <Avatar>{displayName.slice(0, 1)}</Avatar>;
  }

  const failedBeforeRequest = !imageUrl;
  const visibleState = failedBeforeRequest ? 'failed' : loadState;
  return (
    <div className="account-lazy-avatar" ref={containerRef} aria-label={`账号头像：${statusLabel(visibleState)}`}>
      {visibleState === 'loaded' ? null : (
        <Typography.Text type={visibleState === 'failed' ? 'danger' : 'secondary'} className="account-lazy-avatar-status">
          {statusLabel(visibleState)}
        </Typography.Text>
      )}
      {(loadState === 'loading' || loadState === 'loaded') && imageUrl ? (
        <img
          alt={`${displayName}头像`}
          className={loadState === 'loaded' ? 'account-lazy-avatar-image' : 'account-lazy-avatar-image is-loading'}
          src={imageUrl}
          onLoad={() => setLoadState('loaded')}
          onError={() => setLoadState('failed')}
        />
      ) : null}
    </div>
  );
}

export function AccountIdentityCell(props: AccountIdentityCellProps) {
  const nickname = [props.tgFirstName, props.tgLastName].filter(Boolean).join(' ') || '未设置';
  return (
    <Space>
      <AccountLazyAvatar {...props} />
      <Space orientation="vertical" size={0}>
        <Typography.Text strong>{props.displayName}</Typography.Text>
        <Typography.Text type="secondary">username：@{props.username ?? '未设置'} / {props.phone}</Typography.Text>
        <Typography.Text type="secondary">账号分组：{props.poolName}</Typography.Text>
        <Typography.Text type="secondary">昵称：{nickname}</Typography.Text>
      </Space>
    </Space>
  );
}
