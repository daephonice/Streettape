/* EIP-1193: MetaMask, Binance Web3 Wallet, Trust, Rabby. Same MarktapeWallet API. */
(function () {
  const BSC = "0x38";
  const STORAGE_KEY = "mkt_wallet";

  function emit(address) {
    window.dispatchEvent(new CustomEvent("marktape:wallet", { detail: { address } }));
  }

  function getProvider() {
    const eth = window.ethereum;
    if (!eth) return null;
    if (eth.providers && eth.providers.length) {
      return eth.providers.find((p) => p.isMetaMask || p.isBinance) || eth.providers[0];
    }
    return eth;
  }

  function getAddress() {
    const s = window.__marktapeWallet;
    return s && s.address ? s.address : null;
  }

  function shortAddr(a) {
    if (!a) return "";
    return a.slice(0, 6) + "…" + a.slice(-4);
  }

  function setConnected(address, provider) {
    window.__marktapeWallet = { kind: "eip1193", address, provider };
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify({ address })); } catch (_) {}
    emit(address);
    syncButton(address);
    return address;
  }

  function syncButton(address) {
    const btn = document.getElementById("mkt-connect-btn");
    if (!btn) return;
    if (address) {
      btn.textContent = shortAddr(address);
      btn.classList.add("connected");
    } else {
      btn.textContent = btn.dataset.empty || "Connect wallet";
      btn.classList.remove("connected");
    }
  }

  async function ensureChain(provider) {
    try {
      const chain = await provider.request({ method: "eth_chainId" });
      if (chain === BSC) return;
      try {
        await provider.request({ method: "wallet_switchEthereumChain", params: [{ chainId: BSC }] });
      } catch (err) {
        if (err && err.code === 4902) {
          await provider.request({
            method: "wallet_addEthereumChain",
            params: [{
              chainId: BSC,
              chainName: "BNB Smart Chain",
              nativeCurrency: { name: "BNB", symbol: "BNB", decimals: 18 },
              rpcUrls: ["https://bsc-dataseed.binance.org"],
              blockExplorerUrls: ["https://bscscan.com"],
            }],
          });
        }
      }
    } catch (err) {
      console.warn("chain switch failed", err);
    }
  }

  async function connectWallet() {
    const provider = getProvider();
    if (!provider) {
      alert("No BNB Chain wallet found. Install MetaMask, Binance Web3 Wallet, or Trust Wallet.");
      return null;
    }
    try {
      const accounts = await provider.request({ method: "eth_requestAccounts" });
      const address = accounts && accounts[0];
      if (!address) return null;
      await ensureChain(provider);
      if (provider.on) {
        provider.on("accountsChanged", (accs) => {
          if (!accs || !accs[0]) {
            window.__marktapeWallet = null;
            try { localStorage.removeItem(STORAGE_KEY); } catch (_) {}
            emit(null);
            syncButton(null);
          } else {
            setConnected(accs[0], provider);
          }
        });
      }
      return setConnected(address, provider);
    } catch (err) {
      console.error("wallet connect failed", err);
      return null;
    }
  }

  async function connectWithPicker() { return connectWallet(); }
  async function connectByName() { return connectWallet(); }
  function listAvailableWallets() {
    return getProvider() ? [{ name: "Injected wallet" }] : [];
  }

  async function disconnect() {
    window.__marktapeWallet = null;
    try { localStorage.removeItem(STORAGE_KEY); } catch (_) {}
    emit(null);
    syncButton(null);
  }

  async function signTransactionForSend(tx) {
    const state = window.__marktapeWallet;
    if (!state || !state.provider) throw new Error("Wallet not connected");
    if (typeof tx === "string") throw new Error("Expected an EVM transaction object");
    const hash = await state.provider.request({ method: "eth_sendTransaction", params: [tx] });
    return { signature: hash };
  }

  async function signTransactionBase64() {
    throw new Error("Swaps open on PancakeSwap");
  }

  async function signTypedData(typedDataJsonString) {
    const state = window.__marktapeWallet;
    if (!state || !state.provider) throw new Error("Wallet not connected");
    return state.provider.request({
      method: "eth_signTypedData_v4",
      params: [state.address, typedDataJsonString],
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    const btn = document.getElementById("mkt-connect-btn");
    if (btn) btn.addEventListener("click", () => {
      if (getAddress()) disconnect();
      else connectWithPicker();
    });
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (raw && getProvider()) {
        const saved = JSON.parse(raw);
        if (saved.address) {
          getProvider().request({ method: "eth_accounts" }).then((accs) => {
            if (accs && accs[0] && accs[0].toLowerCase() === saved.address.toLowerCase()) {
              setConnected(accs[0], getProvider());
            }
          }).catch(() => {});
        }
      }
    } catch (_) {}
  });

  window.MarktapeWallet = {
    connectWallet,
    connectWithPicker,
    connectByName,
    listAvailableWallets,
    signTransactionBase64,
    signTransactionForSend,
    signTypedData,
    getAddress,
    disconnect,
    getProvider,
  };
})();
