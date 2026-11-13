package com.example.mediacontrol;

import android.app.Activity;
import android.app.AlertDialog;
import android.app.DownloadManager;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.database.Cursor;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.os.Environment;
import android.text.InputType;
import android.util.Base64;
import android.view.Gravity;
import android.webkit.HttpAuthHandler;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;

public class MainActivity extends Activity {
    static final String DEFAULT_BASE = "";
    static final String DEFAULT_UPDATE = "";
    static final String PREFS = "mediacontrol";

    SharedPreferences sp;
    WebView webView;
    String base, user, pass;
    String defBase = DEFAULT_BASE, defUser = "", defPass = "", updateUrl = DEFAULT_UPDATE;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        readDefaults();
        sp = getSharedPreferences(PREFS, MODE_PRIVATE);
        boolean saved = sp.contains("user") && sp.contains("pass");
        base = normalize(sp.getString("base", defBase));
        user = sp.getString("user", defUser);
        pass = sp.getString("pass", defPass);
        if (user.isEmpty() || pass.isEmpty()) {
            showLogin("请输入访问账号");
        } else if (saved) {
            showWeb();
        } else {
            autoLogin();
        }
        getWindow().getDecorView().postDelayed(() -> checkUpdate(false), 3500);
    }

    void readDefaults() {
        try (InputStream in = getAssets().open("defaults.properties")) {
            BufferedReader r = new BufferedReader(new InputStreamReader(in, "UTF-8"));
            String line;
            while ((line = r.readLine()) != null) {
                line = line.trim();
                if (line.isEmpty() || line.startsWith("#")) continue;
                int i = line.indexOf('=');
                if (i <= 0) continue;
                String k = line.substring(0, i).trim();
                String v = line.substring(i + 1).trim();
                if (k.equals("base")) defBase = v;
                else if (k.equals("user")) defUser = v;
                else if (k.equals("pass")) defPass = v;
                else if (k.equals("update")) updateUrl = v;
            }
        } catch (Exception ignored) {
        }
        if (defBase.isEmpty()) defBase = DEFAULT_BASE;
    }

    void autoLogin() {
        TextView tv = new TextView(this);
        tv.setText("正在连接…");
        tv.setTextColor(0xFF9AA4AE);
        tv.setTextSize(16);
        tv.setGravity(Gravity.CENTER);
        FrameLayout f = new FrameLayout(this);
        f.setBackgroundColor(0xFF101418);
        f.addView(tv, new FrameLayout.LayoutParams(-1, -1, Gravity.CENTER));
        setContentView(f);
        new Thread(() -> {
            final int code = verify(base, user, pass);
            runOnUiThread(() -> {
                if (code == 200) {
                    sp.edit().putString("base", base).putString("user", user).putString("pass", pass).apply();
                    showWeb();
                } else if (code == 401) {
                    showLogin("默认密码已失效，请重新输入");
                } else {
                    showLogin("连接失败（" + code + "），检查网络后可重试");
                }
            });
        }, "auto-login").start();
    }

    @Override
    public void onBackPressed() {
        if (webView != null && webView.canGoBack()) {
            webView.goBack();
        } else {
            super.onBackPressed();
        }
    }

    static String normalize(String u) {
        u = u == null ? "" : u.trim();
        if (u.isEmpty()) u = DEFAULT_BASE;
        if (u.isEmpty()) return "";
        if (!u.startsWith("http://") && !u.startsWith("https://")) u = "https://" + u;
        if (!u.endsWith("/")) u += "/";
        return u;
    }

    int dp(int n) {
        return (int) (getResources().getDisplayMetrics().density * n);
    }

    static LinearLayout.LayoutParams lp(int w, int h, int top) {
        LinearLayout.LayoutParams p = new LinearLayout.LayoutParams(w, h);
        p.topMargin = top;
        return p;
    }

    void showLogin(String msg) {
        ScrollView sv = new ScrollView(this);
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        int pad = dp(24);
        box.setPadding(pad, dp(48), pad, pad);
        box.setBackgroundColor(0xFF101418);

        TextView title = new TextView(this);
        title.setText("媒体遥控");
        title.setTextColor(Color.WHITE);
        title.setTextSize(26);
        title.setGravity(Gravity.CENTER);
        box.addView(title, lp(-1, -2, 0));

        TextView sub = new TextView(this);
        sub.setText(msg + "\n服务器、账号密码与网页版一致，只需登录一次。");
        sub.setTextColor(0xFF9AA4AE);
        sub.setTextSize(14);
        sub.setGravity(Gravity.CENTER);
        box.addView(sub, lp(-1, -2, dp(16)));

        final String fillUser = user.isEmpty() ? (defUser.isEmpty() ? "media" : defUser) : user;
        final String fillPass = pass.isEmpty() ? defPass : pass;
        final EditText etBase = field("服务器地址", base, InputType.TYPE_TEXT_VARIATION_URI);
        final EditText etUser = field("账号", fillUser, InputType.TYPE_CLASS_TEXT);
        final EditText etPass = field("密码", fillPass, InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        box.addView(etBase, lp(-1, -2, dp(28)));
        box.addView(etUser, lp(-1, -2, dp(8)));
        box.addView(etPass, lp(-1, -2, dp(8)));

        final Button btn = new Button(this);
        btn.setText("登 录");
        box.addView(btn, lp(-1, dp(48), dp(28)));

        btn.setOnClickListener(v -> {
            final String b = normalize(etBase.getText().toString());
            final String u = etUser.getText().toString().trim();
            final String p = etPass.getText().toString();
            if (b.isEmpty()) {
                Toast.makeText(this, "请填写服务器地址", Toast.LENGTH_SHORT).show();
                return;
            }
            if (u.isEmpty() || p.isEmpty()) {
                Toast.makeText(this, "账号和密码不能为空", Toast.LENGTH_SHORT).show();
                return;
            }
            btn.setEnabled(false);
            btn.setText("验证中…");
            new Thread(() -> {
                final int code = verify(b, u, p);
                runOnUiThread(() -> {
                    btn.setEnabled(true);
                    btn.setText("登 录");
                    if (code == 200) {
                        base = b;
                        user = u;
                        pass = p;
                        sp.edit().putString("base", b).putString("user", u).putString("pass", p).apply();
                        showWeb();
                    } else if (code == 401) {
                        Toast.makeText(this, "账号或密码错误", Toast.LENGTH_SHORT).show();
                    } else {
                        Toast.makeText(this, "连接失败（" + code + "），检查服务器地址或网络", Toast.LENGTH_LONG).show();
                    }
                });
            }, "login").start();
        });

        sv.addView(box);
        setContentView(sv);
    }

    EditText field(String hint, String value, int inputType) {
        EditText e = new EditText(this);
        e.setHint(hint);
        e.setText(value);
        e.setSingleLine(true);
        e.setInputType(inputType);
        e.setTextColor(Color.WHITE);
        e.setHintTextColor(0xFF77808A);
        return e;
    }

    static String basic(String u, String p) {
        return "Basic " + Base64.encodeToString((u + ":" + p).getBytes(), Base64.NO_WRAP);
    }

    static int verify(String baseUrl, String u, String p) {
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL(baseUrl + "api/status").openConnection();
            c.setConnectTimeout(8000);
            c.setReadTimeout(8000);
            c.setRequestProperty("Authorization", basic(u, p));
            c.setRequestProperty("Accept", "application/json");
            return c.getResponseCode();
        } catch (Exception e) {
            return -1;
        } finally {
            if (c != null) c.disconnect();
        }
    }

    void showWeb() {
        FrameLayout root = new FrameLayout(this);
        webView = new WebView(this);
        WebSettings s = webView.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        webView.setBackgroundColor(0xFF101418);
        webView.setWebViewClient(new WebViewClient() {
            @Override
            public void onReceivedHttpAuthRequest(WebView view, HttpAuthHandler handler, String host, String realm) {
                handler.proceed(user, pass);
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, String url) {
                if (url.startsWith("http://") || url.startsWith("https://")) return false;
                try {
                    startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url)));
                } catch (Exception ignored) {
                }
                return true;
            }
        });
        webView.setWebChromeClient(new WebChromeClient());
        root.addView(webView, new FrameLayout.LayoutParams(-1, -1));

        TextView menu = new TextView(this);
        menu.setText("⋮");
        menu.setTextSize(22);
        menu.setTextColor(0xCCFFFFFF);
        menu.setGravity(Gravity.CENTER);
        menu.setBackgroundColor(0x33000000);
        FrameLayout.LayoutParams mp = new FrameLayout.LayoutParams(dp(40), dp(40), Gravity.TOP | Gravity.END);
        mp.topMargin = dp(6);
        mp.rightMargin = dp(6);
        menu.setOnClickListener(v -> openMenu());
        root.addView(menu, mp);

        setContentView(root);
        webView.loadUrl(base);
    }

    void openMenu() {
        new AlertDialog.Builder(this)
                .setItems(new String[]{"刷新页面", "检查更新", "重新登录"}, (d, which) -> {
                    if (which == 0) {
                        webView.reload();
                    } else if (which == 1) {
                        checkUpdate(true);
                    } else {
                        confirmLogout();
                    }
                })
                .show();
    }

    void confirmLogout() {
        new AlertDialog.Builder(this)
                .setTitle("重新登录")
                .setMessage("清除本机保存的账号密码？")
                .setPositiveButton("确定", (d, w) -> {
                    sp.edit().clear().apply();
                    user = "";
                    pass = "";
                    if (webView != null) {
                        webView.destroy();
                        webView = null;
                    }
                    showLogin("请输入访问账号");
                })
                .setNegativeButton("取消", null)
                .show();
    }

    String myVersion() {
        try {
            return getPackageManager().getPackageInfo(getPackageName(), 0).versionName;
        } catch (Exception e) {
            return "0";
        }
    }

    static int verCode(String v) {
        if (v == null) return 0;
        v = v.trim().replaceFirst("^[vV]", "");
        String[] p = v.split("\\.");
        int a = 0, b = 0, c = 0;
        try {
            a = Integer.parseInt(p[0].replaceAll("[^0-9]", ""));
            if (p.length > 1) b = Integer.parseInt(p[1].replaceAll("[^0-9]", ""));
            if (p.length > 2) c = Integer.parseInt(p[2].replaceAll("[^0-9]", ""));
        } catch (Exception ignored) {
        }
        return a * 10000 + b * 100 + c;
    }

    void checkUpdate(final boolean manual) {
        if (updateUrl == null || updateUrl.isEmpty()) {
            if (manual) Toast.makeText(this, "未配置更新源", Toast.LENGTH_SHORT).show();
            return;
        }
        new Thread(() -> {
            String ver = null, url = null, notes = null;
            HttpURLConnection c = null;
            try {
                c = (HttpURLConnection) new URL(updateUrl).openConnection();
                c.setConnectTimeout(6000);
                c.setReadTimeout(6000);
                c.setRequestProperty("Cache-Control", "no-cache");
                if (c.getResponseCode() == 200) {
                    InputStream in = c.getInputStream();
                    ByteArrayOutputStream bos = new ByteArrayOutputStream();
                    byte[] buf = new byte[4096];
                    int n;
                    while ((n = in.read(buf)) > 0) bos.write(buf, 0, n);
                    JSONObject o = new JSONObject(new String(bos.toByteArray(), "UTF-8"));
                    ver = o.optString("version", "");
                    url = o.optString("url", "");
                    notes = o.optString("notes", "");
                }
            } catch (Exception ignored) {
            } finally {
                if (c != null) c.disconnect();
            }
            final String fVer = ver, fUrl = url, fNotes = notes;
            runOnUiThread(() -> {
                String cur = myVersion();
                if (fVer == null || fVer.isEmpty() || fUrl == null || fUrl.isEmpty()) {
                    if (manual) Toast.makeText(this, "检查更新失败，请稍后再试", Toast.LENGTH_SHORT).show();
                    return;
                }
                if (verCode(fVer) <= verCode(cur)) {
                    if (manual) Toast.makeText(this, "已是最新版本 " + cur, Toast.LENGTH_SHORT).show();
                    return;
                }
                new AlertDialog.Builder(this)
                        .setTitle("发现新版本 " + fVer)
                        .setMessage(fNotes == null || fNotes.isEmpty() ? ("当前版本 " + cur) : fNotes)
                        .setPositiveButton("下载更新", (d, w) -> downloadApk(fUrl, fVer))
                        .setNegativeButton("稍后", null)
                        .show();
            });
        }, "update-check").start();
    }

    void downloadApk(final String url, final String ver) {
        try {
            DownloadManager dm = (DownloadManager) getSystemService(Context.DOWNLOAD_SERVICE);
            DownloadManager.Request req = new DownloadManager.Request(Uri.parse(url));
            req.setTitle("媒体遥控 " + ver);
            req.setDescription("正在下载更新…");
            req.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
            req.setDestinationInExternalFilesDir(this, Environment.DIRECTORY_DOWNLOADS, "MediaControl-" + ver + ".apk");
            final long id = dm.enqueue(req);
            Toast.makeText(this, "开始下载 " + ver, Toast.LENGTH_SHORT).show();
            new Thread(() -> {
                DownloadManager.Query q = new DownloadManager.Query().setFilterById(id);
                while (true) {
                    int status = -1;
                    try (Cursor cur = dm.query(q)) {
                        if (cur != null && cur.moveToFirst()) {
                            status = cur.getInt(cur.getColumnIndexOrThrow(DownloadManager.COLUMN_STATUS));
                        }
                    } catch (Exception ignored) {
                    }
                    if (status == DownloadManager.STATUS_SUCCESSFUL) {
                        final Uri uri = dm.getUriForDownloadedFile(id);
                        runOnUiThread(() -> installApk(uri, url));
                        return;
                    }
                    if (status == DownloadManager.STATUS_FAILED) {
                        runOnUiThread(() -> downloadFailed(url));
                        return;
                    }
                    try {
                        Thread.sleep(1500);
                    } catch (InterruptedException e) {
                        return;
                    }
                }
            }, "update-poll").start();
        } catch (Exception e) {
            Toast.makeText(this, "下载启动失败：" + e.getMessage(), Toast.LENGTH_LONG).show();
        }
    }

    void installApk(Uri uri, String fallbackUrl) {
        if (uri == null) {
            downloadFailed(fallbackUrl);
            return;
        }
        Intent i = new Intent(Intent.ACTION_VIEW);
        i.setDataAndType(uri, "application/vnd.android.package-archive");
        i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_GRANT_READ_URI_PERMISSION);
        try {
            startActivity(i);
        } catch (Exception e) {
            downloadFailed(fallbackUrl);
        }
    }

    void downloadFailed(final String url) {
        new AlertDialog.Builder(this)
                .setTitle("下载失败")
                .setMessage("是否用浏览器打开下载链接？")
                .setPositiveButton("浏览器打开", (d, w) -> {
                    try {
                        startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url)));
                    } catch (Exception ignored) {
                    }
                })
                .setNegativeButton("取消", null)
                .show();
    }
}
