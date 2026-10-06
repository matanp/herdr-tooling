-- Sourced by `herdr-compose toggle` into the compose pane's nvim.
local draft = vim.env.HERDR_COMPOSE_DRAFT
local target = vim.env.HERDR_COMPOSE_TARGET or "?"

vim.opt.number = false
vim.opt.relativenumber = false
vim.opt.signcolumn = "no"
vim.opt.laststatus = 2
vim.opt.wrap = true
vim.opt.linebreak = true
vim.opt.swapfile = false
vim.opt.mouse = "a"
vim.bo.filetype = "markdown"
vim.opt.showmode = false
vim.opt.statusline = " compose → " .. target .. " %{mode()==#'i'?'INSERT':''}%=ctrl-s / :wq send · :Hist "

local function send(quitting)
  vim.cmd("silent write")
  if vim.fn.join(vim.fn.getline(1, "$"), ""):match("^%s*$") then
    return quitting or vim.notify("draft is empty", vim.log.levels.WARN)
  end
  local out = vim.fn.system({ "herdr-compose", "send" })
  if vim.v.shell_error ~= 0 then
    -- One line, or a 7-row pane stops at a hit-enter prompt.
    local msg = vim.trim(out):gsub("^herdr%-compose: ", ""):gsub("%s+", " ")
    vim.api.nvim_echo({ { msg:sub(1, vim.o.columns - 12), "ErrorMsg" } }, false, {})
    return false
  end
  vim.api.nvim_buf_set_lines(0, 0, -1, false, {})
  vim.cmd("silent write")
  if not quitting then
    vim.cmd("startinsert")
  end
  return true
end

-- :wq / :x / ZZ send. "Saved since the last edit" is the signal, so :q on a
-- restored draft and :q! after edits both close without sending.
local saved = false
vim.api.nvim_create_autocmd("BufWritePost", { callback = function() saved = true end })
vim.api.nvim_create_autocmd({ "TextChanged", "TextChangedI" },
  { callback = function() saved = false end })
vim.api.nvim_create_autocmd("QuitPre", {
  callback = function()
    if saved and not vim.bo.modified and send(true) == false then
      -- Agent blocked: keep the draft on disk and hold the pane open to say so.
      vim.fn.input("not sent, draft kept — Enter to close ")
    end
  end,
})

-- Recall a previously sent prompt into the buffer (most recent last).
local function hist()
  local path = vim.fn.fnamemodify(draft, ":h:h") .. "/history.jsonl"
  if vim.fn.filereadable(path) == 0 then
    vim.notify("no history yet")
    return
  end
  local items = {}
  for _, line in ipairs(vim.fn.readfile(path)) do
    local ok, row = pcall(vim.json.decode, line)
    if ok then
      table.insert(items, 1, row)
    end
  end
  vim.ui.select(items, {
    prompt = "Sent prompts",
    format_item = function(r)
      return os.date("%m-%d %H:%M ", r.ts) .. r.text:gsub("\n", " ⏎ "):sub(1, 120)
    end,
  }, function(choice)
    if choice then
      vim.api.nvim_buf_set_lines(0, 0, -1, false, vim.split(choice.text, "\n"))
    end
  end)
end

vim.keymap.set({ "n", "i" }, "<C-s>", function() send() end, { desc = "send to agent" })
vim.api.nvim_create_user_command("Send", function() send() end, {})
vim.api.nvim_create_user_command("Hist", hist, {})

vim.cmd("normal! G")
vim.cmd("startinsert!")
