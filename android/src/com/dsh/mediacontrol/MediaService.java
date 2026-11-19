package com.dsh.mediacontrol;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.graphics.drawable.Icon;
import android.media.MediaMetadata;
import android.media.session.MediaSession;
import android.media.session.PlaybackState;
import android.os.Build;
import android.os.IBinder;

/**
 * 前台服务：把网页里的播放状态交给系统媒体会话，锁屏/通知栏/蓝牙耳机按键都能直接控制。
 * 只用平台 API（android.media.session.*），不引入 androidx 依赖。
 */
public class MediaService extends Service {
    static final String CHANNEL_ID = "media_control";
    static final int NOTIF_ID = 4711;
    static final String PREFIX = "com.dsh.mediacontrol.ACTION_";

    /** 由 MainActivity 注入：把系统媒体按键转发进 WebView */
    interface Listener {
        void onAction(String action);
    }

    static volatile Listener listener;
    static volatile MediaService instance;

    MediaSession session;
    String title = "";
    boolean playing = false;

    @Override
    public void onCreate() {
        super.onCreate();
        instance = this;
        session = new MediaSession(this, "MediaControl");
        session.setFlags(MediaSession.FLAG_HANDLES_MEDIA_BUTTONS
                | MediaSession.FLAG_HANDLES_TRANSPORT_CONTROLS);
        session.setCallback(new MediaSession.Callback() {
            @Override
            public void onPlay() {
                fire("play");
            }

            @Override
            public void onPause() {
                fire("pause");
            }

            @Override
            public void onSkipToNext() {
                fire("next");
            }

            @Override
            public void onSkipToPrevious() {
                fire("prev");
            }
        });
        session.setSessionActivity(activityIntent());
        session.setActive(true);
        createChannel();
        startForeground(NOTIF_ID, buildNotification());
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && intent.getAction() != null && intent.getAction().startsWith(PREFIX)) {
            fire(intent.getAction().substring(PREFIX.length()));
        }
        return START_STICKY;
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public void onDestroy() {
        try {
            session.setActive(false);
            session.release();
        } catch (Exception ignored) {
        }
        instance = null;
        super.onDestroy();
    }

    static void touch(Context ctx, String title, boolean playing) {
        try {
            Intent i = new Intent(ctx, MediaService.class);
            if (Build.VERSION.SDK_INT >= 26) ctx.startForegroundService(i);
            else ctx.startService(i);
            MediaService svc = instance;
            if (svc != null) svc.update(title, playing);
        } catch (Exception ignored) {
        }
    }

    void fire(String action) {
        Listener l = listener;
        if (l != null) l.onAction(action);
    }

    void update(String t, boolean p) {
        title = t == null ? "" : t.trim();
        playing = p;
        try {
            long actions = PlaybackState.ACTION_PLAY | PlaybackState.ACTION_PAUSE
                    | PlaybackState.ACTION_PLAY_PAUSE | PlaybackState.ACTION_SKIP_TO_NEXT
                    | PlaybackState.ACTION_SKIP_TO_PREVIOUS;
            PlaybackState st = new PlaybackState.Builder()
                    .setActions(actions)
                    .setState(p ? PlaybackState.STATE_PLAYING : PlaybackState.STATE_PAUSED,
                            PlaybackState.PLAYBACK_POSITION_UNKNOWN, 1f)
                    .build();
            session.setPlaybackState(st);
            MediaMetadata md = new MediaMetadata.Builder()
                    .putString(MediaMetadata.METADATA_KEY_TITLE, title)
                    .putString(MediaMetadata.METADATA_KEY_ARTIST, "B站音乐")
                    .build();
            session.setMetadata(md);
        } catch (Exception ignored) {
        }
        NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        if (nm != null) nm.notify(NOTIF_ID, buildNotification());
    }

    PendingIntent activityIntent() {
        Intent i = new Intent(this, MainActivity.class);
        i.addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP);
        int flags = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= 23) flags |= PendingIntent.FLAG_IMMUTABLE;
        return PendingIntent.getActivity(this, 1, i, flags);
    }

    PendingIntent actionIntent(String action) {
        Intent i = new Intent(this, MediaService.class);
        i.setAction(PREFIX + action);
        int flags = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= 23) flags |= PendingIntent.FLAG_IMMUTABLE;
        return PendingIntent.getService(this, action.hashCode(), i, flags);
    }

    Notification.Action action(int icon, String label, String action) {
        return new Notification.Action.Builder(Icon.createWithResource(this, icon), label,
                actionIntent(action)).build();
    }

    void createChannel() {
        if (Build.VERSION.SDK_INT < 26) return;
        NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        if (nm == null) return;
        NotificationChannel ch = new NotificationChannel(CHANNEL_ID, "媒体控制",
                NotificationManager.IMPORTANCE_LOW);
        ch.setShowBadge(false);
        ch.setLockscreenVisibility(Notification.VISIBILITY_PUBLIC);
        nm.createNotificationChannel(ch);
    }

    Notification buildNotification() {
        Notification.Builder b;
        if (Build.VERSION.SDK_INT >= 26) b = new Notification.Builder(this, CHANNEL_ID);
        else b = new Notification.Builder(this);
        String text = playing ? "正在播放" : "已暂停";
        b.setSmallIcon(android.R.drawable.ic_media_play)
                .setContentTitle(title.isEmpty() ? "媒体遥控" : title)
                .setContentText(text)
                .setContentIntent(activityIntent())
                .setOngoing(playing)
                .setOnlyAlertOnce(true)
                .setShowWhen(false)
                .setVisibility(Notification.VISIBILITY_PUBLIC)
                .addAction(action(android.R.drawable.ic_media_previous, "上一首", "prev"));
        if (playing) {
            b.addAction(action(android.R.drawable.ic_media_pause, "暂停", "pause"));
        } else {
            b.addAction(action(android.R.drawable.ic_media_play, "播放", "play"));
        }
        b.addAction(action(android.R.drawable.ic_media_next, "下一首", "next"));
        b.setStyle(new Notification.MediaStyle()
                .setMediaSession(session.getSessionToken())
                .setShowActionsInCompactView(0, 1, 2));
        return b.build();
    }
}
