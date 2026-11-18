CFLAGS ?= -O2 -Wall
CPPFLAGS = -Iinclude

app: src/main.o src/calc.o src/util.o
	$(CC) $(LDFLAGS) -o $@ $^ -lm

src/%.o: src/%.c
	$(CC) $(CPPFLAGS) $(CFLAGS) -c $< -o $@

clean:
	rm -f app src/*.o
