//======================================================================
// DeckReader.hpp - fixed format keyword deck parser
//
// Created  30-Nov-1992  R. Salcedo
// Revised  05-Aug-1994  SWOF / SGOF tables forwarded to KRLOAD
// Revised  14-Jul-1998  INCLUDE nesting raised to 4 levels
//
// Reads the .DATA card image decks the simulator has consumed since
// 1989. Keyword driven, column sensitive in the RUNSPEC section only.
// Comments start with two dashes. A slash terminates a record.
//
// The reader mutates GLOBAL Fortran state as it parses: GRID cards
// go straight into GRDSET, PROPS cards into KRINI/KRLOAD, so parsing
// a deck twice is NOT idempotent and parsing two decks in one process
// leaves you with a mixture of both.
//======================================================================
#ifndef PETRO_DECK_READER_HPP
#define PETRO_DECK_READER_HPP

#include "Units.hpp"

class FluidModel;

// Deck sections, in the order the parser expects them.
enum DeckSection {
    SECTION_NONE    = 0,
    SECTION_RUNSPEC = 1,
    SECTION_GRID    = 2,
    SECTION_PROPS   = 3,
    SECTION_SOLUTION = 4,
    SECTION_SCHEDULE = 5
};

class DeckReader {
public:
    DeckReader();
    ~DeckReader();

    // Parse a deck file. Returns 0 on success, or the line number of
    // the first error. Call error_text() for a description.
    int read(const char* filename);

    int nx() const;
    int ny() const;
    int nz() const;
    int cell_count() const;
    int unit_system() const;
    int well_count() const;

    double api_gravity() const;
    double gas_gravity() const;
    double reservoir_temperature() const;

    // Non zero once a RUNSPEC section has been seen and the grid
    // dimensions are known. SIMINI must not be called before this.
    int has_dimensions() const;

    const char* error_text() const;
    int error_line() const;

private:
    DeckReader(const DeckReader& other);
    DeckReader& operator=(const DeckReader& other);

    int handle_keyword(const char* keyword);
    int read_real_array(double* target, int count);
    int read_swof_table();
    int read_welspecs();
    int read_tstep();
    int push_include(const char* filename);
    int pop_include();

    // Card image scanning. The 1992 reader worked on 80 column cards
    // and nothing here has been widened since.
    int  next_card();
    void strip_comment(char* card) const;
    int  split_fields(char* card);

    struct FileStack;
    FileStack* m_stack;

    UnitConverter m_units;

    int m_nx;
    int m_ny;
    int m_nz;
    int m_section;
    int m_wells;
    int m_error_line;

    double m_api;
    double m_gas_sg;
    double m_temp_f;

    char m_card[132];
    char m_fields[16][32];
    int  m_nfields;
    char m_error[128];
};

#endif // PETRO_DECK_READER_HPP
