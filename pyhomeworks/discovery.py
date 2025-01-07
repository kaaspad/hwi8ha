class HomeworksDiscovery:
    def __init__(self, controller: Homeworks):
        self._controller = controller
        self._responses = {}
        self._discovered_devices = {}
        self._discovery_lock = asyncio.Lock()
        self._stop_discovery = False
        self._total_addresses = 0
        self._progress_callback = None

    def _handle_response(self, msg_type: str, values: list[Any]) -> None:
        """Handle responses from the controller."""
        if msg_type in ["DL", "CCOS", "CCIS"]:
            addr = values[0]
            state = values[1]
            self._responses[addr] = {
                "type": msg_type,
                "state": state
            }

    async def discover_devices(
        self,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        start_addr: str = "1",
        end_addr: str = "255"
    ) -> dict[str, dict[str, Any]]:
        """Discover devices in the specified address range."""
        if progress_callback is not None:
            self._progress_callback = progress_callback
            self._discovered_devices.clear()
            self._responses.clear()
            
            # Set up response handler
            self._controller.register_callback(self._handle_response)

            addresses = self._generate_addresses(start_addr, end_addr)
            self._total_addresses = len(addresses) * 3  # multiply by 3 for light, CCO, CCI

            for addr in addresses:
                await self._discover_light(addr)
                await self._discover_cco(addr)
                await self._discover_cci(addr)

                self._update_progress()

            # Remove callback handler
            self._controller.unregister_callback(self._handle_response)

            return self._discovered_devices

    def stop_discovery(self) -> None:
        """Stop the discovery process."""
        self._stop_discovery = True

    async def _discover_light(self, addr: str) -> bool:
        """Try to discover a light at the given address."""
        self._responses.pop(addr, None)
        self._controller.request_dimmer_level(addr)
        
        # Wait for response with timeout
        for _ in range(5):  # 0.5 second timeout
            await asyncio.sleep(0.1)
            if addr in self._responses and self._responses[addr]["type"] == "DL":
                return True
        return False

    async def _discover_cco(self, addr: str) -> bool:
        """Try to discover a CCO at the given address."""
        self._responses.pop(addr, None)
        self._controller.request_cco_state(addr)
        
        # Wait for response with timeout
        for _ in range(5):  # 0.5 second timeout
            await asyncio.sleep(0.1)
            if addr in self._responses and self._responses[addr]["type"] == "CCOS":
                return True
        return False

    async def _discover_cci(self, addr: str) -> bool:
        """Try to discover a CCI at the given address."""
        self._responses.pop(addr, None)
        self._controller.request_cci_state(addr)
        
        # Wait for response with timeout
        for _ in range(5):  # 0.5 second timeout
            await asyncio.sleep(0.1)
            if addr in self._responses and self._responses[addr]["type"] == "CCIS":
                return True
        return False
