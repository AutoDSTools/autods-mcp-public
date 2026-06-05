-- Atomic multi-bucket check-and-consume for the per-user rate limiter (F1).
-- Mirrors ``evaluate_buckets`` in ratelimit.py line-for-line.
--
-- KEYS:  one Redis hash per bucket ({tokens, ts}).
-- ARGV:  now, cost, then (capacity, refill_rate) per bucket in KEYS order.
-- Return: {allowed (1/0), retry_after_ms}.
local now = tonumber(ARGV[1])
local cost = tonumber(ARGV[2])
local n = #KEYS
local allowed = 1
local retry_ms = 0
local refilled = {}
for i = 1, n do
  local cap = tonumber(ARGV[3 + (i - 1) * 2])
  local rate = tonumber(ARGV[4 + (i - 1) * 2])
  local data = redis.call('HMGET', KEYS[i], 'tokens', 'ts')
  local tokens = tonumber(data[1])
  local ts = tonumber(data[2])
  if tokens == nil then tokens = cap; ts = now end
  local elapsed = now - ts
  if elapsed < 0 then elapsed = 0 end
  tokens = math.min(cap, tokens + elapsed * rate)
  refilled[i] = tokens
  if tokens < cost then
    allowed = 0
    local rm = math.ceil((cost - tokens) / rate * 1000)
    if rm > retry_ms then retry_ms = rm end
  end
end
for i = 1, n do
  local cap = tonumber(ARGV[3 + (i - 1) * 2])
  local rate = tonumber(ARGV[4 + (i - 1) * 2])
  local tokens = refilled[i]
  if allowed == 1 then tokens = tokens - cost end
  redis.call('HSET', KEYS[i], 'tokens', tostring(tokens), 'ts', tostring(now))
  redis.call('EXPIRE', KEYS[i], math.ceil(cap / rate) + 1)
end
return {allowed, retry_ms}
