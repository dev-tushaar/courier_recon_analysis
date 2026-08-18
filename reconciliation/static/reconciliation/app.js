/*
  Front-end behaviour, jQuery.

  Two jobs:
    1. Filter the discrepancy table without a page reload.
    2. Update a discrepancy's status in place.

  Both talk to the JSON endpoints in views.py. Django requires a CSRF token on
  any unsafe method, so it is read from the cookie once and attached to every
  POST via $.ajaxSetup rather than threaded through each call by hand.
*/

(function ($) {
  "use strict";

  // --- CSRF -------------------------------------------------------------

  function getCookie(name) {
    var match = document.cookie.match(new RegExp("(^|;\\s*)" + name + "=([^;]*)"));
    return match ? decodeURIComponent(match[2]) : null;
  }

  var csrftoken = getCookie("csrftoken");

  $.ajaxSetup({
    beforeSend: function (xhr, settings) {
      // Only attach the token to state-changing, same-origin requests.
      if (!/^(GET|HEAD|OPTIONS|TRACE)$/.test(settings.type) && !this.crossDomain) {
        xhr.setRequestHeader("X-CSRFToken", csrftoken);
      }
    }
  });

  // --- Helpers ----------------------------------------------------------

  function formatRupees(value) {
    var n = parseFloat(value);
    if (isNaN(n)) { return value; }
    // Indian digit grouping: last three digits, then pairs (12,34,567.89).
    var sign = n < 0 ? "-" : "";
    var fixed = Math.abs(n).toFixed(2);
    var parts = fixed.split(".");
    var whole = parts[0];
    var lastThree = whole.slice(-3);
    var rest = whole.slice(0, -3);
    if (rest) {
      lastThree = "," + lastThree;
      rest = rest.replace(/\B(?=(\d{2})+(?!\d))/g, ",");
    }
    return sign + "\u20b9" + rest + lastThree + "." + parts[1];
  }

  function escapeHtml(text) {
    return $("<div>").text(text == null ? "" : text).html();
  }

  function debounce(fn, wait) {
    var timer = null;
    return function () {
      var ctx = this, args = arguments;
      clearTimeout(timer);
      timer = setTimeout(function () { fn.apply(ctx, args); }, wait);
    };
  }

  // --- Discrepancy table ------------------------------------------------

  var $table = $("#discrepancy-table");
  if (!$table.length) { return; }

  var endpoint = $table.data("endpoint");
  var statusOptions = $table.data("status-options") || [];
  var statusEndpoint = $table.data("status-endpoint");
  // Public demo: the API rejects writes, so render a static label rather than a
  // dropdown that would only ever fail.
  var readOnly = $table.data("readonly") === true || $table.data("readonly") === "true";
  var $body = $table.find("tbody");
  var $count = $("#result-count");
  var $total = $("#result-total");
  var inFlight = null;

  function buildStatusSelect(row) {
    if (readOnly) {
      return $("<span>", {
        "class": "tag tag-" + row.status,
        text: row.status_label || row.status
      });
    }
    var $select = $("<select>", {
      "class": "status-select form-input btn-sm",
      "data-id": row.id,
      "aria-label": "Status for " + row.awb
    });
    $.each(statusOptions, function (_, opt) {
      $("<option>", {
        value: opt[0],
        text: opt[1],
        selected: opt[0] === row.status
      }).appendTo($select);
    });
    // Remember the saved value so a failed request can roll the control back
    // instead of leaving the UI showing a status the database never accepted.
    $select.data("previous", row.status);
    return $select;
  }

  function renderRow(row) {
    var $tr = $("<tr>", { "data-id": row.id });

    $("<td>", { "class": "awb", text: row.awb }).appendTo($tr);

    $("<td>").append(
      $("<span>", { "class": "tag tag-" + row.kind, text: row.kind_label })
    ).appendTo($tr);

    $("<td>", { "class": "num", text: formatRupees(row.billed) }).appendTo($tr);
    $("<td>", {
      "class": "num",
      text: row.expected === "-" ? "\u2014" : formatRupees(row.expected)
    }).appendTo($tr);

    var impact = parseFloat(row.impact);
    $("<td>", {
      "class": "num " + (impact > 0 ? "over" : impact < 0 ? "under" : ""),
      text: impact === 0 ? "\u2014" : (impact > 0 ? "+" : "") + formatRupees(row.impact)
    }).appendTo($tr);

    $("<td>", { "class": "detail-cell", text: row.detail }).appendTo($tr);
    $("<td>").append(buildStatusSelect(row)).appendTo($tr);

    return $tr;
  }

  function render(data) {
    $body.empty();

    if (!data.rows.length) {
      $("<tr>").append(
        $("<td>", {
          colspan: 7,
          "class": "empty",
          html: "<strong>Nothing matches these filters</strong>Widen the filters to see more findings."
        })
      ).appendTo($body);
    } else {
      $.each(data.rows, function (_, row) { $body.append(renderRow(row)); });
    }

    $count.text(data.count + (data.count === 1 ? " finding" : " findings"));
    var total = parseFloat(data.total_impact);
    $total
      .text(formatRupees(data.total_impact))
      .removeClass("over under")
      .addClass(total > 0 ? "over" : total < 0 ? "under" : "");
  }

  function load() {
    // Abort the previous request so a slow early response cannot overwrite a
    // fast later one -- otherwise fast typing leaves stale rows on screen.
    if (inFlight) { inFlight.abort(); }

    var params = {
      kind: $("#filter-kind").val(),
      status: $("#filter-status").val(),
      q: $("#filter-search").val(),
      min_impact: $("#filter-min").val()
    };

    inFlight = $.getJSON(endpoint, params)
      .done(render)
      .fail(function (xhr, textStatus) {
        if (textStatus === "abort") { return; }
        $body.html(
          '<tr><td colspan="7" class="empty"><strong>Could not load findings</strong>' +
          'The server did not respond. Reload the page to try again.</td></tr>'
        );
      })
      .always(function () { inFlight = null; });
  }

  $("#filter-kind, #filter-status").on("change", load);
  $("#filter-search, #filter-min").on("input", debounce(load, 250));

  $("#filter-reset").on("click", function () {
    $("#filter-kind, #filter-status").val("");
    $("#filter-search, #filter-min").val("");
    load();
  });

  // Delegated handler: rows are replaced on every filter, so binding directly
  // to each select would lose the handler after the first re-render.
  $body.on("change", ".status-select", function () {
    var $select = $(this);
    var $row = $select.closest("tr");
    var previous = $select.data("previous") || "";

    $select.prop("disabled", true);

    // The template renders the route with a 0 id; swap the whole path segment
    // rather than the first "0" character, which would corrupt an id like 101.
    var url = statusEndpoint.replace("/0/", "/" + $select.data("id") + "/");

    $.post(url, { status: $select.val() })
      .done(function () {
        $select.data("previous", $select.val());
        $row.addClass("row-flash");
        setTimeout(function () { $row.removeClass("row-flash"); }, 700);
      })
      .fail(function () {
        if (previous) { $select.val(previous); }
        window.alert("Status did not save. Check your connection and try again.");
      })
      .always(function () { $select.prop("disabled", false); });
  });

  load();
})(jQuery);
