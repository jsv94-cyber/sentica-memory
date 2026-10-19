#!/bin/sh
# Build a new chain and swap the jump. A leftover SENTICA_NEW is renamed
# aside so its jump keeps dropping, every old jump is removed, and the
# old chain is flushed only after DOCKER-USER no longer jumps to it.
set -eu
# An unreferenced holding chain can be removed. A referenced one is the
# live chain from an earlier park and stays until the replacement jump
# is inserted below.
if iptables -L SENTICA_KEEP -n >/dev/null 2>&1; then
  if ! iptables -C DOCKER-USER -j SENTICA_KEEP >/dev/null 2>&1; then
    iptables -F SENTICA_KEEP
    iptables -X SENTICA_KEEP
  fi
fi
if iptables -L SENTICA_OLD -n >/dev/null 2>&1; then
  if ! iptables -C DOCKER-USER -j SENTICA_OLD >/dev/null 2>&1; then
    iptables -F SENTICA_OLD
    iptables -X SENTICA_OLD
  fi
fi
# Park a live SENTICA_NEW. -E retargets the jump, so drops remain while
# the name is freed for the replacement chain.
if iptables -L SENTICA_NEW -n >/dev/null 2>&1; then
  if iptables -L SENTICA_KEEP -n >/dev/null 2>&1; then
    iptables -E SENTICA_NEW SENTICA_OLD
  else
    iptables -E SENTICA_NEW SENTICA_KEEP
  fi
fi
iptables -N SENTICA_NEW
iptables -A SENTICA_NEW -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
iptables -A SENTICA_NEW -s 10.8.0.50/32 -d 1.1.1.1/32 -p udp --dport 53 -j RETURN
iptables -A SENTICA_NEW -s 10.8.0.50/32 -d 1.1.1.1/32 -p tcp --dport 53 -j RETURN
iptables -A SENTICA_NEW -s 10.8.0.50/32 -p tcp --dport 443 -j RETURN
iptables -A SENTICA_NEW -s 10.8.0.18/32 -j DROP
iptables -A SENTICA_NEW -s 10.8.0.19/32 -j DROP
iptables -A SENTICA_NEW -s 10.8.0.20/32 -j DROP
iptables -A SENTICA_NEW -s 10.8.0.22/32 -j DROP
iptables -A SENTICA_NEW -s 10.8.0.82/32 -j DROP
iptables -A SENTICA_NEW -s 10.8.0.83/32 -j DROP
iptables -A SENTICA_NEW -s 10.8.0.84/32 -j DROP
iptables -A SENTICA_NEW -s 10.8.0.85/32 -j DROP
iptables -A SENTICA_NEW -s 10.8.0.34/32 -j DROP
iptables -A SENTICA_NEW -s 10.8.0.35/32 -j DROP
iptables -A SENTICA_NEW -s 10.8.0.50/32 -j DROP
iptables -A SENTICA_NEW -s 10.8.0.66/32 -j DROP
iptables -A SENTICA_NEW -s 10.8.0.67/32 -j DROP
iptables -I DOCKER-USER 1 -j SENTICA_NEW
while iptables -D DOCKER-USER -j SENTICA 2>/dev/null; do :; done
while iptables -D DOCKER-USER -j SENTICA_KEEP 2>/dev/null; do :; done
while iptables -D DOCKER-USER -j SENTICA_OLD 2>/dev/null; do :; done
iptables -F SENTICA 2>/dev/null || true
iptables -X SENTICA 2>/dev/null || true
iptables -F SENTICA_KEEP 2>/dev/null || true
iptables -X SENTICA_KEEP 2>/dev/null || true
iptables -F SENTICA_OLD 2>/dev/null || true
iptables -X SENTICA_OLD 2>/dev/null || true
iptables -E SENTICA_NEW SENTICA
