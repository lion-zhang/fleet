# Waking machines

A machine that is asleep, or switched off with Wake-on-LAN armed, can be woken from
anywhere, as long as one other fleet machine on its network is up.

> **You:** wake the gpu tower and start the eval there

```bash
fleet wake gpu-tower              # send the packet, wait for it to answer (3 minutes at most)
fleet wake gpu-tower --no-wait    # send it and return
fleet wake gpu-tower --via nas    # send it from this machine on the tower's network
fleet wake gpu-tower --json       # for scripts and agents
```

`fleet ls` marks an offline machine fleet can wake, and `fleet doctor` suggests it for one
that does not answer:

```text
· asleep? gpu-tower can be woken: fleet wake gpu-tower
```

Your agent asks you before waking anything: it switches on your hardware.

## How it works

A Wake-on-LAN "magic packet" is a broadcast on the machine's own network. It never
crosses the internet or a tailnet, and a sleeping machine is not on the tailnet anyway.
So fleet sends it from a machine that is on that network:

1. **This machine**, when it is on the same network, for example the laptop at home.
2. Otherwise **another fleet machine on that network that is up**, over ssh, connecting
   as `fleet ssh` does. Nothing is installed there: it runs one `python3` command (or
   the `wakeonlan` tool, or PowerShell on Windows). If that machine does not answer,
   fleet tries the next one, up to three.

Two machines are on the same network when they have the same router. Every probe
records the router's hardware address, so two homes that both use `192.168.1.0/24` are
told apart. A machine that fleet never reached is not chosen to send the packet.

The packet is sent to UDP ports 9 and 7, three times, to the network's broadcast address
and to `255.255.255.255`.

What fleet knows of the sleeping machine (its network cards' hardware addresses, its
subnet, and whether each card is armed to wake) was read by its last probe, while it was
up. A machine added while it was asleep has to be probed awake once (`fleet ls -r NAME`)
before it can be woken.

`fleet wake` then probes the machine every few seconds until it answers ssh. A machine
that answers but refuses the key is awake; `fleet doctor NAME` says what to do about the
key.

## Turning Wake-on-LAN on

fleet reports whether a card is armed, and changes nothing. If every card says it is
off, `fleet wake` refuses and says what to change:

- **BIOS/UEFI:** enable "Wake on LAN", "Power On By PCI-E" or similar. Without this,
  nothing below helps. To wake a machine that was switched off, also disable "ErP" or
  "Deep Sleep" if the board has it.
- **Linux:** `sudo ethtool -s eno1 wol g`, and make it stick: with NetworkManager,
  `nmcli connection modify CONNECTION 802-3-ethernet.wake-on-lan magic`. fleet reads it
  from `/sys/class/net/*/device/power/wakeup`, which needs no root.
- **macOS:** System Settings → Battery (or Energy) → *Wake for network access*.
- **Windows:** Device Manager → the network adapter → Power Management → *Allow this
  device to wake the computer* and *Only allow a magic packet to wake the computer*.

Wake-on-LAN works over a wired connection. Most Wi-Fi cards cannot wake a machine.

## Naming the card

The probe sends the packet to every card that is not known to be unarmed, preferring
those that had an address. To name the card yourself, put it in the machine's own
`machine.yaml` (see [What is on each machine](resources.md)):

```yaml
wake:
  mac: 02:00:00:00:00:02     # or a list of them
```

## What cannot be woken

- Rentals and cloud VMs: start them from the provider's console or command line. A cloud
  VM's virtual card is reported as unable to wake.
- A machine with no other fleet machine up on its network: wake one there by hand, keep
  a small always-on machine there (a NAS or a Raspberry Pi added to fleet), or name one
  with `--via`.
- A machine that has lost power (unplugged, or a power cut, unless its BIOS powers on
  when power returns).
