(function () {
  "use strict";

  var DEFAULT_PAGE_SIZE = 100;
  var PAGE_SIZES = [50, 100, 200, 500];
  var STORAGE_DATA_KEY = "unsafe-doc-data:" + location.pathname;
  var SIDEBAR_KEY = "unsafe-doc-sidebar:" + location.pathname;
  var state = {
    records: [],
    filtered: [],
    crates: [],
    moduleCounts: {},
    selectedModule: "",
    selectedTypes: new Set(),
    allTypes: [],
    safetyOnly: false,
    llmReviewOnly: false,
    diffOnly: false,
    page: 1,
    pageSize: DEFAULT_PAGE_SIZE,
    saved: {}
  };

  var tbody = document.getElementById("apiRows");
  var summary = document.getElementById("summary");
  var loading = document.getElementById("loading");
  var tableWrap = document.querySelector(".unsafe-table-wrap");
  var pagerTop = document.getElementById("pagerTop");
  var pagerBottom = document.getElementById("pagerBottom");
  var saveTimer = null;

  function hasOwn(object, key) {
    return Object.prototype.hasOwnProperty.call(object, key);
  }

  function loadSavedData() {
    try {
      state.saved = JSON.parse(localStorage.getItem(STORAGE_DATA_KEY) || "{}") || {};
    } catch (_error) {
      state.saved = {};
    }
  }

  function queueSavedData() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(function () {
      try {
        localStorage.setItem(STORAGE_DATA_KEY, JSON.stringify(state.saved));
      } catch (_error) {}
    }, 250);
  }

  function setSavedValue(key, value, initialValue) {
    if (value === initialValue || value === "") delete state.saved[key];
    else state.saved[key] = value;
    queueSavedData();
  }

  function loadURLState() {
    var params = new URLSearchParams(location.search);
    var types = params.get("t");
    if (types !== null) {
      state.selectedTypes = new Set(types ? types.split(",") : []);
    }
    state.safetyOnly = params.get("s") === "1";
    state.llmReviewOnly = params.get("l") === "1";
    state.diffOnly = params.get("d") === "1";
    state.selectedModule = params.get("m") || "";

    var requestedSize = Number(params.get("ps"));
    if (PAGE_SIZES.indexOf(requestedSize) !== -1) state.pageSize = requestedSize;
    var requestedPage = Number(params.get("p"));
    if (Number.isInteger(requestedPage) && requestedPage > 0) state.page = requestedPage;

    document.getElementById("safetyFilter").checked = state.safetyOnly;
    document.getElementById("llmReviewFilter").checked = state.llmReviewOnly;
    document.getElementById("diffFilter").checked = state.diffOnly;
  }

  function updateURL() {
    var params = new URLSearchParams();
    if (state.selectedTypes.size < state.allTypes.length) {
      params.set("t", state.allTypes.filter(function (type) {
        return state.selectedTypes.has(type);
      }).join(","));
    }
    if (state.selectedModule) params.set("m", state.selectedModule);
    if (state.safetyOnly) params.set("s", "1");
    if (state.llmReviewOnly) params.set("l", "1");
    if (state.diffOnly) params.set("d", "1");
    if (state.page > 1) params.set("p", String(state.page));
    if (state.pageSize !== DEFAULT_PAGE_SIZE) params.set("ps", String(state.pageSize));
    var query = params.toString();
    history.replaceState(null, "", location.pathname + (query ? "?" + query : "") + location.hash);
  }

  function buildTypeFilters() {
    var counts = {};
    state.records.forEach(function (record) {
      counts[record.kind] = (counts[record.kind] || 0) + 1;
    });
    state.allTypes = Object.keys(counts).sort();
    if (state.selectedTypes.size === 0 && !new URLSearchParams(location.search).has("t")) {
      state.selectedTypes = new Set(state.allTypes);
    }
    var container = document.getElementById("typeFilters");
    container.replaceChildren();
    state.allTypes.forEach(function (type) {
      var label = document.createElement("label");
      label.className = "type-item";
      var input = document.createElement("input");
      input.type = "checkbox";
      input.dataset.type = type;
      input.checked = state.selectedTypes.has(type);
      label.append(input, " " + type + " (" + counts[type] + ")");
      container.appendChild(label);
    });
  }

  function makeTreeModel() {
    var root = { children: new Map() };
    Object.keys(state.moduleCounts).forEach(function (path) {
      var node = root;
      var parts = path.split("::");
      parts.forEach(function (part, index) {
        if (!node.children.has(part)) {
          node.children.set(part, {
            name: part,
            path: parts.slice(0, index + 1).join("::"),
            children: new Map()
          });
        }
        node = node.children.get(part);
      });
    });
    return root;
  }

  function createTreeNode(node) {
    var li = document.createElement("li");
    var children = Array.from(node.children.values()).sort(function (left, right) {
      if (Boolean(left.children.size) !== Boolean(right.children.size)) return left.children.size ? -1 : 1;
      return left.name.localeCompare(right.name);
    });
    if (children.length) {
      var toggle = document.createElement("span");
      toggle.className = "tree-toggle expanded";
      toggle.dataset.toggle = node.path;
      toggle.textContent = "▾";
      li.appendChild(toggle);
    }
    var label = document.createElement("span");
    label.className = "tree-node" + (state.selectedModule === node.path ? " selected" : "");
    label.dataset.module = node.path;
    label.append(document.createTextNode(node.name + " "));
    var count = document.createElement("span");
    count.className = "tree-count";
    count.textContent = "(" + state.moduleCounts[node.path] + ")";
    label.appendChild(count);
    li.appendChild(label);
    if (children.length) {
      var ul = document.createElement("ul");
      children.forEach(function (child) { ul.appendChild(createTreeNode(child)); });
      li.appendChild(ul);
    }
    return li;
  }

  function buildModuleTree() {
    var tree = document.getElementById("moduleTree");
    tree.replaceChildren();
    var allLi = document.createElement("li");
    var all = document.createElement("span");
    all.className = "tree-node tree-node-all" + (state.selectedModule ? "" : " selected");
    all.dataset.module = "";
    all.textContent = "Show All ";
    var allCount = document.createElement("span");
    allCount.className = "tree-count";
    allCount.textContent = "(" + state.records.length + ")";
    all.appendChild(allCount);
    allLi.appendChild(all);
    tree.appendChild(allLi);
    var model = makeTreeModel();
    var rootNodes = Array.from(model.children.values());
    rootNodes.sort(function (left, right) {
      var leftIndex = state.crates.indexOf(left.name);
      var rightIndex = state.crates.indexOf(right.name);
      if (leftIndex === -1) leftIndex = Number.MAX_SAFE_INTEGER;
      if (rightIndex === -1) rightIndex = Number.MAX_SAFE_INTEGER;
      return leftIndex - rightIndex || left.name.localeCompare(right.name);
    });
    rootNodes.forEach(function (node) {
      tree.appendChild(createTreeNode(node));
    });
  }

  function recordMatches(record) {
    var moduleMatches = !state.selectedModule || record.module_path === state.selectedModule ||
      record.module_path.indexOf(state.selectedModule + "::") === 0;
    return state.selectedTypes.has(record.kind) && moduleMatches &&
      (!state.safetyOnly || !record.has_safety) &&
      (!state.llmReviewOnly || record.has_review) &&
      (!state.diffOnly || record.has_diff);
  }

  function countMissingDocs(records) {
    var grouped = new Map();
    records.forEach(function (record) {
      var origin = record.trait_origin || record.id;
      if (!grouped.has(origin)) grouped.set(origin, true);
      if (record.has_safety) grouped.set(origin, false);
    });
    var count = 0;
    grouped.forEach(function (missing) { if (missing) count += 1; });
    return count;
  }

  function appendTextCell(row, value, asCode) {
    var td = document.createElement("td");
    if (asCode) {
      var code = document.createElement("code");
      code.textContent = value;
      td.appendChild(code);
    } else td.textContent = value;
    row.appendChild(td);
    return td;
  }

  function updateDiffEditor(row) {
    var select = row.querySelector(".diff-classification");
    var items = row.querySelector(".diff-items");
    if (!select || !items) return;
    select.classList.remove("diff-incorrect", "diff-missing");
    if (select.value === "Incorrect") select.classList.add("diff-incorrect");
    if (select.value === "Missing") select.classList.add("diff-missing");
    items.hidden = select.value === "Correct";
    items.setAttribute("aria-label", select.value + " items");
    items.placeholder = "List " + select.value.toLowerCase() + " items, one per line";
  }

  function createRow(record) {
    var row = document.createElement("tr");
    row.dataset.id = record.id;
    appendTextCell(row, String(record.index), false);
    appendTextCell(row, record.module_path, true);

    var apiCell = document.createElement("td");
    var code = document.createElement("code");
    code.textContent = record.api_name;
    if (record.url) {
      var link = document.createElement("a");
      link.href = record.url;
      link.appendChild(code);
      apiCell.appendChild(link);
    } else apiCell.appendChild(code);
    row.appendChild(apiCell);
    appendTextCell(row, record.kind, false);

    var safety = document.createElement("td");
    safety.innerHTML = record.safety_html || "";
    row.appendChild(safety);
    var review = document.createElement("td");
    review.className = "llm-review-cell";
    review.innerHTML = record.review_html || "";
    row.appendChild(review);
    var diff = document.createElement("td");
    diff.className = "llm-diff-cell";
    diff.innerHTML = record.diff_html || "";
    row.appendChild(diff);

    var tagsCell = document.createElement("td");
    var tags = document.createElement("textarea");
    tags.className = "tags-input";
    tags.placeholder = "tags";
    tags.rows = 1;
    tags.value = hasOwn(state.saved, record.id + ":t") ? state.saved[record.id + ":t"] : record.auto_tags;
    tags.dataset.initial = record.auto_tags || "";
    tagsCell.appendChild(tags);
    row.appendChild(tagsCell);

    var notesCell = document.createElement("td");
    var notes = document.createElement("textarea");
    notes.className = "notes-input";
    notes.placeholder = "notes";
    notes.rows = 1;
    notes.value = state.saved[record.id + ":n"] || "";
    if (notes.value.trim()) row.classList.add("row-confirmed");
    notesCell.appendChild(notes);
    row.appendChild(notesCell);

    var diffClass = row.querySelector(".diff-classification");
    var diffItems = row.querySelector(".diff-items");
    if (diffClass) {
      diffClass.dataset.initial = diffClass.value;
      if (state.saved[record.id + ":dc"]) diffClass.value = state.saved[record.id + ":dc"];
    }
    if (diffItems) {
      diffItems.dataset.initial = diffItems.value;
      if (hasOwn(state.saved, record.id + ":di")) diffItems.value = state.saved[record.id + ":di"];
    }
    updateDiffEditor(row);
    return row;
  }

  function pagerMarkup(totalPages) {
    var parts = [];
    parts.push('<button data-page="1"' + (state.page === 1 ? " disabled" : "") + '>« First</button>');
    parts.push('<button data-page="' + Math.max(1, state.page - 1) + '"' + (state.page === 1 ? " disabled" : "") + '>‹ Prev</button>');
    var start = Math.max(1, state.page - 2);
    var end = Math.min(totalPages, state.page + 2);
    if (start > 1) parts.push("<span>…</span>");
    for (var page = start; page <= end; page += 1) {
      parts.push('<button data-page="' + page + '" class="' + (page === state.page ? "active" : "") + '">' + page + "</button>");
    }
    if (end < totalPages) parts.push("<span>…</span>");
    parts.push('<button data-page="' + Math.min(totalPages, state.page + 1) + '"' + (state.page === totalPages ? " disabled" : "") + '>Next ›</button>');
    parts.push('<button data-page="' + totalPages + '"' + (state.page === totalPages ? " disabled" : "") + '>Last »</button>');
    parts.push('<span class="pager-spacer">Page ' + state.page + " / " + totalPages + "</span>");
    parts.push('<label class="pager-spacer">Per page <select data-page-size>' + PAGE_SIZES.map(function (size) {
      return '<option value="' + size + '"' + (size === state.pageSize ? " selected" : "") + ">" + size + "</option>";
    }).join("") + "</select></label>");
    return parts.join("");
  }

  function renderPage() {
    var totalPages = Math.max(1, Math.ceil(state.filtered.length / state.pageSize));
    if (state.page > totalPages) state.page = totalPages;
    var start = (state.page - 1) * state.pageSize;
    var end = Math.min(start + state.pageSize, state.filtered.length);
    var fragment = document.createDocumentFragment();
    state.filtered.slice(start, end).forEach(function (record) {
      fragment.appendChild(createRow(record));
    });
    tbody.replaceChildren(fragment);
    var range = state.filtered.length ? (start + 1) + "–" + end : "0";
    summary.textContent = "Showing " + range + " of " + state.filtered.length +
      " matching items; " + state.records.length + " total; " +
      countMissingDocs(state.filtered) + " APIs need safety docs after grouping trait methods";
    var markup = pagerMarkup(totalPages);
    pagerTop.innerHTML = markup;
    pagerBottom.innerHTML = markup;
    updateURL();
  }

  function applyFilters(resetPage) {
    if (resetPage) state.page = 1;
    state.filtered = state.records.filter(recordMatches);
    renderPage();
  }

  function setupFilters() {
    document.getElementById("typeFilters").addEventListener("change", function (event) {
      var type = event.target.dataset.type;
      if (!type) return;
      if (event.target.checked) state.selectedTypes.add(type);
      else state.selectedTypes.delete(type);
      applyFilters(true);
    });
    document.getElementById("safetyFilter").addEventListener("change", function () {
      state.safetyOnly = this.checked; applyFilters(true);
    });
    document.getElementById("llmReviewFilter").addEventListener("change", function () {
      state.llmReviewOnly = this.checked; applyFilters(true);
    });
    document.getElementById("diffFilter").addEventListener("change", function () {
      state.diffOnly = this.checked; applyFilters(true);
    });
  }

  function setupTree() {
    document.getElementById("moduleTree").addEventListener("click", function (event) {
      var toggle = event.target.closest(".tree-toggle");
      if (toggle) {
        var childList = toggle.parentElement.querySelector(":scope > ul");
        if (childList) {
          var collapsed = childList.hidden = !childList.hidden;
          toggle.classList.toggle("collapsed", collapsed);
        }
        return;
      }
      var node = event.target.closest(".tree-node");
      if (!node) return;
      state.selectedModule = node.dataset.module || "";
      document.querySelectorAll(".tree-node.selected").forEach(function (selected) {
        selected.classList.remove("selected");
      });
      node.classList.add("selected");
      applyFilters(true);
    });
  }

  function setupPager(container) {
    container.addEventListener("click", function (event) {
      var button = event.target.closest("button[data-page]");
      if (!button || button.disabled) return;
      state.page = Number(button.dataset.page);
      renderPage();
      tableWrap.scrollIntoView({ block: "start" });
    });
    container.addEventListener("change", function (event) {
      if (!event.target.matches("select[data-page-size]")) return;
      state.pageSize = Number(event.target.value);
      state.page = 1;
      renderPage();
      tableWrap.scrollIntoView({ block: "start" });
    });
  }

  function setupRowEditing() {
    tbody.addEventListener("input", function (event) {
      var row = event.target.closest("tr");
      if (!row) return;
      var id = row.dataset.id;
      if (event.target.matches(".tags-input")) {
        setSavedValue(id + ":t", event.target.value, event.target.dataset.initial || "");
      } else if (event.target.matches(".notes-input")) {
        setSavedValue(id + ":n", event.target.value, "");
        row.classList.toggle("row-confirmed", Boolean(event.target.value.trim()));
      } else if (event.target.matches(".diff-items")) {
        setSavedValue(id + ":di", event.target.value, event.target.dataset.initial || "");
      }
    });
    tbody.addEventListener("change", function (event) {
      if (!event.target.matches(".diff-classification")) return;
      var row = event.target.closest("tr");
      setSavedValue(row.dataset.id + ":dc", event.target.value, event.target.dataset.initial || "");
      updateDiffEditor(row);
    });
  }

  function setupSidebar() {
    var layout = document.querySelector(".layout");
    var sidebar = document.querySelector(".sidebar");
    var resizer = document.querySelector(".sidebar-resizer");
    var width = 280;
    try {
      var saved = JSON.parse(localStorage.getItem(SIDEBAR_KEY) || "null");
      if (saved && saved.w >= 160) { width = saved.w; sidebar.style.width = width + "px"; }
      if (saved && saved.hidden) layout.classList.add("sidebar-hidden");
    } catch (_error) {}
    function saveSidebar() {
      try { localStorage.setItem(SIDEBAR_KEY, JSON.stringify({ w: width, hidden: layout.classList.contains("sidebar-hidden") })); } catch (_error) {}
    }
    document.getElementById("sidebarToggle").addEventListener("click", function () { layout.classList.add("sidebar-hidden"); saveSidebar(); });
    document.getElementById("sidebarFab").addEventListener("click", function () { layout.classList.remove("sidebar-hidden"); saveSidebar(); });
    resizer.addEventListener("mousedown", function (event) {
      var startX = event.clientX;
      var startWidth = width;
      function move(moveEvent) {
        width = Math.max(160, Math.min(800, startWidth + moveEvent.clientX - startX));
        sidebar.style.width = width + "px";
      }
      function up() {
        document.removeEventListener("mousemove", move);
        document.removeEventListener("mouseup", up);
        saveSidebar();
      }
      document.addEventListener("mousemove", move);
      document.addEventListener("mouseup", up);
    });
  }

  function setupColumnResize() {
    var table = tableWrap.querySelector("table");
    var columns = table.querySelectorAll("col");
    table.querySelectorAll("th").forEach(function (header, index) {
      var handle = document.createElement("div");
      handle.className = "col-resize-handle";
      header.appendChild(handle);
      handle.addEventListener("mousedown", function (event) {
        event.preventDefault();
        var startX = event.clientX;
        var startWidth = header.getBoundingClientRect().width;
        function move(moveEvent) {
          var nextWidth = startWidth + moveEvent.clientX - startX;
          if (nextWidth > 40) columns[index].style.width = nextWidth + "px";
        }
        function up() {
          document.removeEventListener("mousemove", move);
          document.removeEventListener("mouseup", up);
        }
        document.addEventListener("mousemove", move);
        document.addEventListener("mouseup", up);
      });
    });
  }

  async function init() {
    loadSavedData();
    setupSidebar();
    setupFilters();
    setupTree();
    setupPager(pagerTop);
    setupPager(pagerBottom);
    setupRowEditing();
    setupColumnResize();
    try {
      var response = await fetch(new URL("unsafe-apis.json", document.baseURI));
      if (!response.ok) throw new Error("HTTP " + response.status);
      var payload = await response.json();
      state.records = payload.records || [];
      state.moduleCounts = payload.module_counts || {};
      state.crates = payload.crates || [];
      loadURLState();
      buildTypeFilters();
      buildModuleTree();
      loading.hidden = true;
      summary.hidden = false;
      pagerTop.hidden = false;
      pagerBottom.hidden = false;
      tableWrap.hidden = false;
      applyFilters(false);
    } catch (error) {
      loading.classList.add("error");
      loading.textContent = "Could not load unsafe-apis.json: " + error.message + ". Serve this directory over HTTP instead of opening index.html directly.";
    }
  }

  init();
}());
