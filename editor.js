/* Repositionnement manuel des installations + enregistrement protégé par mot de passe.
 *
 * Les corrections sont stockées dans data/positions.json du dépôt GitHub.
 * L'écriture passe par l'API GitHub avec un jeton (fine-grained, « Contents: read and write »
 * sur ce seul dépôt). Ce jeton n'est jamais publié en clair : il est chiffré (AES-GCM,
 * clé dérivée du mot de passe par PBKDF2-SHA256) dans data/editor.json. Sans le bon mot de
 * passe, la page ne peut pas le déchiffrer, donc pas écrire.
 */
"use strict";
const EDIT = (() => {
  const host = location.hostname;
  const OWNER = host.endsWith(".github.io") ? host.split(".")[0] : "CBT135";
  const REPO = host.endsWith(".github.io") ? (location.pathname.split("/")[1] || "carte-pv-bess") : "carte-pv-bess";
  const API = `https://api.github.com/repos/${OWNER}/${REPO}/contents/`;
  const ITER = 250000;
  const keyOf = p => p.eic || `${p.nom}|${p.insee}`;

  let POS = {};            // corrections enregistrées
  let pending = {};        // corrections en cours (null = retour à la position d'origine)
  let on = false, drag = null;

  // ------------------------------------------------------------ utilitaires
  const b64e = buf => btoa(String.fromCharCode(...new Uint8Array(buf)));
  const b64d = s => Uint8Array.from(atob(s), c => c.charCodeAt(0));
  const utf8b64 = s => btoa(unescape(encodeURIComponent(s)));
  const b64utf8 = s => decodeURIComponent(escape(atob(s.replace(/\n/g, ""))));
  async function deriveKey(pw, salt){
    const base = await crypto.subtle.importKey("raw", new TextEncoder().encode(pw), "PBKDF2", false, ["deriveKey"]);
    return crypto.subtle.deriveKey({name:"PBKDF2", salt, iterations:ITER, hash:"SHA-256"}, base,
      {name:"AES-GCM", length:256}, false, ["encrypt","decrypt"]);
  }
  async function encryptToken(token, pw){
    const salt = crypto.getRandomValues(new Uint8Array(16)), iv = crypto.getRandomValues(new Uint8Array(12));
    const ct = await crypto.subtle.encrypt({name:"AES-GCM", iv}, await deriveKey(pw, salt), new TextEncoder().encode(token));
    return {v:1, kdf:"PBKDF2-SHA256", iter:ITER, salt:b64e(salt), iv:b64e(iv), ct:b64e(ct)};
  }
  async function decryptToken(blob, pw){
    try{
      const pt = await crypto.subtle.decrypt({name:"AES-GCM", iv:b64d(blob.iv)},
        await deriveKey(pw, b64d(blob.salt)), b64d(blob.ct));
      return new TextDecoder().decode(pt);
    }catch(e){ throw new Error("Mot de passe incorrect."); }
  }
  async function gh(path, token, opts={}){
    const r = await fetch(API + path + (opts.method ? "" : `?ref=main&t=${Date.now()}`), {
      ...opts, headers:{Accept:"application/vnd.github+json", Authorization:`Bearer ${token}`,
        "X-GitHub-Api-Version":"2022-11-28", ...(opts.headers||{})}});
    if (r.status === 404 && !opts.method) return null;
    if (!r.ok){
      const j = await r.json().catch(()=>({}));
      if (r.status === 401) throw new Error("Jeton GitHub refusé (expiré ou révoqué) : refaites la configuration.");
      if (r.status === 403) throw new Error("Jeton GitHub sans droit d'écriture sur ce dépôt.");
      throw new Error(`GitHub ${r.status} : ${j.message || r.statusText}`);
    }
    return r.json();
  }
  async function putFile(path, text, sha, token, message){
    return gh(path, token, {method:"PUT", body:JSON.stringify({message, content:utf8b64(text), sha:sha||undefined, branch:"main"})});
  }

  // ------------------------------------------------------------ chargement
  async function load(){
    try{
      const r = await fetch("data/positions.json", {cache:"no-cache"});
      POS = r.ok ? await r.json() : {};
    }catch(e){ POS = {}; }
  }
  function applyTo(data){
    data.forEach(p => {
      const c = POS[keyOf(p)];
      if (c){ p._orig = p._orig || [p.lat, p.lon, p.loc_src]; p.lat = c.lat; p.lon = c.lon; p.loc_src = "manuel"; p.pos_date = c.date; }
    });
  }

  // ------------------------------------------------------------ interface
  const css = `
  .ed-btn{background:#fff;border:0;border-radius:8px;box-shadow:0 2px 10px #0f172a2e;font:600 13px Inter,system-ui,sans-serif;
    padding:8px 12px;cursor:pointer;color:#18212b}
  .ed-btn.on{background:#18212b;color:#fff}
  #edBar{position:absolute;left:50%;bottom:24px;transform:translateX(-50%);z-index:1500;display:none;align-items:center;gap:10px;
    background:#18212b;color:#fff;border-radius:12px;padding:10px 12px 10px 16px;font:13px Inter,system-ui,sans-serif;box-shadow:0 8px 28px #0f172a55;max-width:calc(100% - 24px)}
  #edBar button{font:600 13px Inter,system-ui,sans-serif;border-radius:8px;padding:7px 12px;cursor:pointer;border:1px solid #ffffff44;background:transparent;color:#fff}
  #edBar button.primary{background:#f2a20c;border-color:#f2a20c;color:#18212b}
  #edBar button:disabled{opacity:.4;cursor:default}
  #edModal{position:fixed;inset:0;background:#0f172a66;z-index:3000;display:none;align-items:center;justify-content:center;padding:16px}
  #edModal .box{background:#fff;border-radius:14px;padding:20px;width:100%;max-width:400px;font:14px/1.45 Inter,system-ui,sans-serif;color:#18212b}
  #edModal h2{font-size:16px;margin:0 0 10px}
  #edModal label{display:block;font-size:12px;font-weight:600;color:#5d6b7a;margin:12px 0 4px}
  #edModal input{width:100%;font:inherit;padding:9px 10px;border:1px solid #e2e6eb;border-radius:8px}
  #edModal .help{font-size:12px;color:#5d6b7a;margin-top:6px}
  #edModal .row{display:flex;gap:8px;justify-content:flex-end;margin-top:16px}
  #edModal .row button{font:600 13px Inter,system-ui,sans-serif;border-radius:8px;padding:9px 14px;cursor:pointer;border:1px solid #e2e6eb;background:#fff}
  #edModal .row button.primary{background:#18212b;color:#fff;border-color:#18212b}
  #edMsg{margin-top:10px;font-size:13px}
  #edMsg.err{color:#b42318} #edMsg.ok{color:#1a7f37}
  .leaflet-container.ed-on{cursor:default}
  .leaflet-container.ed-drag{cursor:grabbing!important}`;
  document.head.insertAdjacentHTML("beforeend", `<style>${css}</style>`);
  document.body.insertAdjacentHTML("beforeend", `
    <div id="edBar"><span id="edTxt"></span>
      <button type="button" id="edUndo">Annuler</button>
      <button type="button" id="edSave" class="primary">Enregistrer…</button></div>
    <div id="edModal"><div class="box">
      <h2>Enregistrer les positions</h2>
      <div id="edSetup" style="display:none">
        <div class="help">Première utilisation : collez un jeton GitHub <i>fine-grained</i> limité au dépôt
          <b>${OWNER}/${REPO}</b> avec la permission <b>Contents : Read and write</b>. Il sera chiffré avec le mot de passe
          et stocké dans <code>data/editor.json</code> ; il ne sera plus demandé ensuite.</div>
        <label for="edTok">Jeton GitHub</label><input id="edTok" type="password" autocomplete="off" placeholder="github_pat_…">
      </div>
      <label for="edPw">Mot de passe</label><input id="edPw" type="password" autocomplete="current-password">
      <div id="edMsg"></div>
      <div class="row"><button type="button" id="edCancel">Fermer</button><button type="button" id="edGo" class="primary">Enregistrer</button></div>
    </div></div>`);

  const ctl = L.control({position:"topleft"});
  ctl.onAdd = () => {
    const b = L.DomUtil.create("button", "ed-btn");
    b.type = "button"; b.textContent = "✥ Repositionner"; b.id = "edToggle";
    b.title = "Déplacer les installations à la souris";
    L.DomEvent.disableClickPropagation(b);
    b.onclick = () => toggle();
    return b;
  };
  ctl.addTo(map);

  const n = () => Object.keys(pending).length;
  function refreshBar(){
    $("edBar").style.display = on ? "flex" : "none";
    $("edTxt").textContent = n() ? `${n()} modification(s) non enregistrée(s)` :
      "Glissez un point pour le déplacer · clic droit = position d'origine";
    $("edUndo").disabled = $("edSave").disabled = !n();
  }
  function toggle(force){
    if (on && n() && force === undefined && !confirm("Quitter le mode repositionnement ? Les modifications non enregistrées restent en attente.")) return;
    on = force !== undefined ? force : !on;
    $("edToggle").classList.toggle("on", on);
    map.getContainer().classList.toggle("ed-on", on);
    refreshBar();
  }
  function styleMoved(m){
    const moved = m._p.loc_src === "manuel" || pending[keyOf(m._p)] !== undefined;
    m.setStyle(moved ? {color:"#18212b", weight:1.6, dashArray:null} : {color:"#ffffff", weight:.8});
    m._moved = moved;
  }

  // ------------------------------------------------------------ glisser-déposer
  function attach(ms){
    ms.forEach(m => {
      styleMoved(m);
      m.on("mousedown", e => {
        if (!on) return;
        L.DomEvent.stop(e);
        drag = m; m.closeTooltip();
        map.dragging.disable();
        map.getContainer().classList.add("ed-drag");
      });
      m.on("contextmenu", e => {
        if (!on) return;
        L.DomEvent.stop(e);
        const p = m._p, k = keyOf(p);
        if (!(POS[k] || pending[k])) return;
        if (!confirm(`Revenir à la position d'origine pour « ${p.nom} » ?`)) return;
        if (pending[k] && !POS[k]) { delete pending[k]; } else { pending[k] = null; }
        if (p._orig){ [p.lat, p.lon] = p._orig; p.loc_src = p._orig[2]; m.setLatLng([p.lat, p.lon]); }
        styleMoved(m); refreshBar();
      });
      m.off("mouseout"); m.on("mouseout", () => { if (!m._moved) m.setStyle({color:"#ffffff", weight:.8}); else styleMoved(m); });
    });
  }
  map.on("mousemove", e => { if (drag) drag.setLatLng(e.latlng); });
  const endDrag = () => {
    if (!drag) return;
    const m = drag, p = m._p, ll = m.getLatLng();
    drag = null;
    map.dragging.enable();
    map.getContainer().classList.remove("ed-drag");
    if (!p._orig) p._orig = [p.lat, p.lon, p.loc_src];
    p.lat = +ll.lat.toFixed(6); p.lon = +ll.lng.toFixed(6); p.loc_src = "manuel";
    pending[keyOf(p)] = {lat:p.lat, lon:p.lon, nom:p.nom, insee:p.insee};
    styleMoved(m); refreshBar();
  };
  map.on("mouseup", endDrag);
  document.addEventListener("mouseup", endDrag);
  window.addEventListener("beforeunload", e => { if (n()) { e.preventDefault(); e.returnValue = ""; } });

  $("edUndo").onclick = () => {
    if (!confirm("Annuler toutes les modifications non enregistrées ?")) return;
    markers.forEach(m => {
      const p = m._p, k = keyOf(p);
      if (pending[k] === undefined) return;
      const c = POS[k];
      if (c){ p.lat = c.lat; p.lon = c.lon; p.loc_src = "manuel"; }
      else if (p._orig){ [p.lat, p.lon] = p._orig; p.loc_src = p._orig[2]; }
      m.setLatLng([p.lat, p.lon]);
    });
    pending = {}; markers.forEach(styleMoved); refreshBar();
  };

  // ------------------------------------------------------------ enregistrement
  let editorBlob;
  $("edSave").onclick = async () => {
    $("edMsg").textContent = ""; $("edMsg").className = "";
    $("edPw").value = "";
    try{ const r = await fetch(`data/editor.json?t=${Date.now()}`, {cache:"no-store"}); editorBlob = r.ok ? await r.json() : null; }
    catch(e){ editorBlob = null; }
    $("edSetup").style.display = editorBlob ? "none" : "block";
    $("edModal").style.display = "flex";
    setTimeout(() => (editorBlob ? $("edPw") : $("edTok")).focus(), 50);
  };
  $("edCancel").onclick = () => { $("edModal").style.display = "none"; };
  $("edPw").addEventListener("keydown", e => { if (e.key === "Enter") $("edGo").click(); });
  $("edGo").onclick = async () => {
    const pw = $("edPw").value, msg = $("edMsg");
    const say = (t, cls="") => { msg.textContent = t; msg.className = cls; };
    if (!pw) return say("Saisissez le mot de passe.", "err");
    $("edGo").disabled = true;
    try{
      let token;
      if (editorBlob){
        say("Vérification du mot de passe…");
        token = await decryptToken(editorBlob, pw);
      } else {
        token = $("edTok").value.trim();
        if (!token) throw new Error("Collez le jeton GitHub.");
        say("Configuration de l'enregistrement…");
        const blob = await encryptToken(token, pw);
        const cur = await gh("data/editor.json", token);
        await putFile("data/editor.json", JSON.stringify(blob, null, 1) + "\n", cur && cur.sha, token,
          "Éditeur web : configuration de l'enregistrement");
        editorBlob = blob;
      }
      say("Enregistrement…");
      const cur = await gh("data/positions.json", token);
      const remote = cur ? JSON.parse(b64utf8(cur.content) || "{}") : {};
      const today = new Date().toISOString().slice(0,10);
      Object.entries(pending).forEach(([k, v]) => {
        if (v === null) delete remote[k];
        else remote[k] = {lat:v.lat, lon:v.lon, date:today, nom:v.nom, insee:v.insee};
      });
      const sorted = Object.fromEntries(Object.keys(remote).sort().map(k => [k, remote[k]]));
      await putFile("data/positions.json", JSON.stringify(sorted, null, 1) + "\n", cur && cur.sha, token,
        `Positions : ${n()} correction(s) via l'éditeur web`);
      POS = sorted; pending = {};
      markers.forEach(styleMoved); refreshBar();
      say("Enregistré. Visible par tous après le redéploiement GitHub Pages (≈ 1 min).", "ok");
      setTimeout(() => { $("edModal").style.display = "none"; }, 2200);
    }catch(e){
      say(e.message || String(e), "err");
    }finally{
      $("edGo").disabled = false; $("edPw").value = "";
    }
  };

  return {load, applyTo, attach, keyOf};
})();
